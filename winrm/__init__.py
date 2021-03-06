from __future__ import unicode_literals

import xml.etree.ElementTree as ET
from base64 import b64encode

import re

from winrm.protocol import Protocol

# Feature support attributes for multi-version clients.
# These values can be easily checked for with hasattr(winrm, "FEATURE_X"),
# "'auth_type' in winrm.FEATURE_SUPPORTED_AUTHTYPES", etc for clients to sniff features
# supported by a particular version of pywinrm
FEATURE_SUPPORTED_AUTHTYPES = ['basic', 'certificate', 'ntlm', 'kerberos', 'plaintext',
                               'ssl', 'credssp']
FEATURE_READ_TIMEOUT = True
FEATURE_OPERATION_TIMEOUT = True


class Response(object):
    """Response from a remote command execution"""

    def __init__(self, args):
        self.std_out, self.std_err, self.status_code = args

    def __repr__(self):
        # TODO put tree dots at the end if out/err was truncated
        return '<Response code {0}, out "{1}", err "{2}">'.format(
            self.status_code, self.std_out[:20], self.std_err[:20])


class Session(object):
    maxEnvelopeSize = 2000

    # TODO implement context manager methods
    def __init__(self, target, auth, **kwargs):
        username, password = auth
        self.url = self._build_url(target, kwargs.get('transport', 'plaintext'))
        self.protocol = Protocol(self.url,
                                 username=username, password=password, **kwargs)

    def run_cmd(self, command, args=()):
        # TODO optimize perf. Do not call open/close shell every time
        shell_id = self.protocol.open_shell()
        command_id = self.protocol.run_command(shell_id, command, args)
        rs = Response(self.protocol.get_command_output(shell_id, command_id))
        self.protocol.cleanup_command(shell_id, command_id)
        self.protocol.close_shell(shell_id)
        return rs

    def run_ps_long(self, script):
        """ Run PowerShell Long
        
        Base64 encodes a Powershell script and executes the powershell
        encoded script command
        """

        shell_id = self.protocol.open_shell()

        def run_command(command, useResultLogger=True):
            command_id = self.protocol.run_command(shell_id, command)
            rs = Response(self.protocol.get_command_output(
                shell_id, command_id, useResultLogger=useResultLogger))
            self.protocol.cleanup_command(shell_id, command_id)

            # Powershell errors are returned in XML, clean them up
            if len(rs.std_err):
                rs.std_err = self._clean_error_msg(rs.std_err)
            return rs

        def make_ps_command(ps_script):
            return ("powershell -encodedcommand %s"
                    % b64encode(ps_script.encode("utf_16_le")))

        def run_and_check_ps(command, stage_message, useResultLogger=True):
            rs = run_command(command, useResultLogger=useResultLogger)
            if len(rs.std_err) or rs.status_code != 0:
                self.protocol.close_shell(shell_id)
                raise Exception("%s\n%s" % (stage_message, rs.std_err))
            return rs.std_out

        # Get the name of a temp file
        cmd = ("$script_file = [IO.Path]::GetTempFileName() | "
               " Rename-Item -NewName { $_ -replace 'tmp$', 'tmp.ps1' } -PassThru\n"
               '"$script_file"')
        script_file = run_and_check_ps(make_ps_command(cmd),
                                       "Creating temp script file",
                                       useResultLogger=False)
        script_file = script_file.strip()

        # Append the data to the file
        base64_script = b64encode(script)
        for chunk_index in range(0, len(base64_script), self.maxEnvelopeSize):
            chunk = base64_script[chunk_index:chunk_index + self.maxEnvelopeSize]
            cmd = 'ECHO %s %s "%s" ' % (chunk,
                                        ('>>' if chunk_index else '>'),
                                        script_file)
            run_and_check_ps(cmd,
                             "writing chunk %s to temp script file" % chunk_index)

        # Execute the powershell script
        cmd = '''
            # Convert it from b64 encoded
            $b64 = get-content "%(script_file)s"
            [System.Text.Encoding]::ASCII.GetString([System.Convert]::FromBase64String($b64)) |
                out-file -Encoding Default "%(script_file)s"
        ''' % {'script_file': script_file}
        run_and_check_ps(make_ps_command(cmd),
                         "Converting temp script file back from b64 encoding")

        cmd = ("""PowerShell.exe -ExecutionPolicy Bypass -Command "& '%s' " """
               % script_file)
        rs = run_command(cmd)

        # Finally, cleanup the temp file
        cmd = "remove-item '%s' " % script_file
        run_and_check_ps(make_ps_command(cmd), "Deleting temp script file")

        self.protocol.close_shell(shell_id)

        return rs

    def run_ps(self, script):
        """base64 encodes a Powershell script and executes the powershell
        encoded script command
        """
        # must use utf16 little endian on windows
        encoded_ps = b64encode(script.encode('utf_16_le')).decode('ascii')

        # There is an issue with powershell scripts over 2k or 8k (platform dependent)
        # You can not have a command line + argument longer than this
        if len(encoded_ps) > self.maxEnvelopeSize:
            return self.run_ps_long(script)

        rs = self.run_cmd('powershell -encodedcommand {0}'.format(encoded_ps))
        if len(rs.std_err):
            # if there was an error message, clean it it up and make it human
            # readable
            rs.std_err = self._clean_error_msg(rs.std_err)
        return rs

    def _clean_error_msg(self, msg):
        """converts a Powershell CLIXML message to a more human readable string
        """
        # TODO prepare unit test, beautify code
        # if the msg does not start with this, return it as is
        if msg.startswith("#< CLIXML\r\n"):
            # for proper xml, we need to remove the CLIXML part
            # (the first line)
            msg_xml = msg[11:]
            try:
                # remove the namespaces from the xml for easier processing
                msg_xml = self._strip_namespace(msg_xml)
                root = ET.fromstring(msg_xml)
                # the S node is the error message, find all S nodes
                nodes = root.findall("./S")
                new_msg = ""
                for s in nodes:
                    # append error msg string to result, also
                    # the hex chars represent CRLF so we replace with newline
                    new_msg += s.text.replace("_x000D__x000A_", "\n")
            except Exception as e:
                # if any of the above fails, the msg was not true xml
                # print a warning and return the orignal string
                # TODO do not print, raise user defined error instead
                print("Warning: there was a problem converting the Powershell"
                      " error message: %s" % (e))
            else:
                # if new_msg was populated, that's our error message
                # otherwise the original error message will be used
                if len(new_msg):
                    # remove leading and trailing whitespace while we are here
                    msg = new_msg.strip()
        return msg

    def _strip_namespace(self, xml):
        """strips any namespaces from an xml string"""
        try:
            p = re.compile("xmlns=*[\"\"][^\"\"]*[\"\"]")
            allmatches = p.finditer(xml)
            for match in allmatches:
                xml = xml.replace(match.group(), "")
            return xml
        except Exception as e:
            raise Exception(e)

    @staticmethod
    def _build_url(target, transport):
        match = re.match(
            '(?i)^((?P<scheme>http[s]?)://)?(?P<host>[0-9a-z-_.]+)(:(?P<port>\d+))?(?P<path>(/)?(wsman)?)?',
            target)  # NOQA
        scheme = match.group('scheme')
        if not scheme:
            # TODO do we have anything other than HTTP/HTTPS
            scheme = 'https' if transport == 'ssl' else 'http'
        host = match.group('host')
        port = match.group('port')
        if not port:
            port = 5986 if transport == 'ssl' else 5985
        path = match.group('path')
        if not path:
            path = 'wsman'
        return '{0}://{1}:{2}/{3}'.format(scheme, host, port, path.lstrip('/'))
