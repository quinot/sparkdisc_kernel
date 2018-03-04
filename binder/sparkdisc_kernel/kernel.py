# Adapted from https://github.com/sourceryinstitute/jupyter-CAF-kernel

from colorama import Fore, Style
from queue import Queue
from threading import Thread

from ipykernel.kernelbase import Kernel
import subprocess
import tempfile
import os
from shlex import split as shsplit

msg_prefix = "[SPARK Discovery kernel] "

class RealTimeSubprocess(subprocess.Popen):
    """
    A subprocess that allows to read its stdout and stderr in real time
    """

    def __init__(self, cmd, write_to_stdout, write_to_stderr):
        """
        :param cmd: the command to execute
        :param write_to_stdout: a callable that will be called with chunks of data from stdout
        :param write_to_stderr: a callable that will be called with chunks of data from stderr
        """
        self._write_to_stdout = write_to_stdout
        self._write_to_stderr = write_to_stderr

        super().__init__(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)

        self._stdout_queue = Queue()
        self._stdout_thread = Thread(target=RealTimeSubprocess._enqueue_output, args=(self.stdout, self._stdout_queue))
        self._stdout_thread.daemon = True
        self._stdout_thread.start()

        self._stderr_queue = Queue()
        self._stderr_thread = Thread(target=RealTimeSubprocess._enqueue_output, args=(self.stderr, self._stderr_queue))
        self._stderr_thread.daemon = True
        self._stderr_thread.start()

    @staticmethod
    def _enqueue_output(stream, queue):
        """
        Add chunks of data from a stream to a queue until the stream is empty.
        """
        for line in iter(lambda: stream.read(4096), b''):
            queue.put(line)
        stream.close()

    def write_contents(self):
        """
        Write the available content from stdin and stderr where specified when the instance was created
        :return:
        """

        def read_all_from_queue(queue):
            res = b''
            size = queue.qsize()
            while size != 0:
                res += queue.get_nowait()
                size -= 1
            return res

        stdout_contents = read_all_from_queue(self._stdout_queue)
        if stdout_contents:
            self._write_to_stdout(stdout_contents)
        stderr_contents = read_all_from_queue(self._stderr_queue)
        if stderr_contents:
            self._write_to_stderr(stderr_contents)


class SPARKDiscKernel(Kernel):
    implementation = 'sparkdisc_kernel'
    implementation_version = '0.1'
    language = 'SPARK'
    language_version = 'SPARK 2014'
    language_info = {'name': 'spark',
                     'mimetype': 'text/plain',
                     'file_extension': '.ada'}
    banner = "SPARK Discovery kernel.\n" \
             "Creates source code files and executables in a temporary folder.\n"


    def __init__(self, *args, **kwargs):
        super(SPARKDiscKernel, self).__init__(*args, **kwargs)
        self.tmpdir = tempfile.TemporaryDirectory()

    def _write_to_stdout(self, contents):
        self.send_response(self.iopub_socket, 'stream', {'name': 'stdout', 'text': contents})

    def _write_to_stderr(self, contents):
        self.send_response(self.iopub_socket, 'stream', {'name': 'stderr', 'text': contents})

    def create_jupyter_subprocess(self, cmd):
        return RealTimeSubprocess(cmd,
                                  lambda contents: self._write_to_stdout(contents.decode()),
                                  lambda contents: self._write_to_stderr(contents.decode()))

    def runcmd(self, cmd):
        p = self.create_jupyter_subprocess(cmd)
        while p.poll() is None:
            p.write_contents()
        p.write_contents()
        return p.returncode

    def _filter_magics(self, code):

        magics = {'cflags': [],
                  'ldflags': [],
                  'args': [],
                }

        for line in code.splitlines():
            line = line.strip()
            if line.startswith('%') and ":" in line:
                key, value = line.strip('%').split(":", 2)
                key = key.lower()

                if key in ['ldflags', 'fcflags', 'args']:
                    magics[key] = shsplit(value)
                else:
                    pass # need to add exception handling

        return magics

    def init_metadata(self, parent):
        self.metadata = super(SPARKDiscKernel, self).init_metadata(parent)
        self.metadata.update(parent["content"].get("all_cell_metadata", {}))
        return self.metadata

    def msg(self, msg, *args, **kwargs):
        color = kwargs.pop("color")
        self._write_to_stdout((color if color else "") +
                              msg_prefix +
                              msg.format(kwargs) +
                              (Style.RESET_ALL if color else ""))

    def err(self, msg, *args, **kwargs):
        self._write_to_stderr(msg_prefix + msg.format(*args, **kwargs))

    def fail(self, msg, *args, **kwargs):
        self.err(msg, *args, **kwargs)
        return {'status': 'ok',
                'execution_count': self.execution_count,
                'payload': [],
                'user_expressions': {}}

    def do_execute(self, code, silent, store_history=True,
                   user_expressions=None, allow_stdin=False):

        magics = self._filter_magics(code)
        os.chdir(self.tmpdir.name)

        # Show received metadata (debug)

        if False:
            self.err("execute_request metadata: {0}", self.metadata)

        # Initialize context files

        for cf_name, cf_contents in self.metadata.get("context_files", {}).items():
            with open(cf_name, "w") as cf:
                cf.write(cf_contents)

        with open('src_%d.ada' % self.execution_count, "w") as source_file:
            for line in code.splitlines():
                if line.startswith(('%', '%%', '$', '?')):
                    continue
                source_file.write(line + '\n')
            source_file.flush()

            # Chop input

            rc = self.runcmd(["gnatchop", "-w", "-q", source_file.name])
            if rc != 0:
                return self.fail("gnatchop exited with status {0}", rc)

        # Call prover

        rc = self.runcmd(['gnatprove', '-P', 'main'])
        if rc != 0:
            return self.fail("gnatprove exited with status {0}", rc)

        # Call builder and run program

        if "run" in self.metadata.get("tags", []):
            rc = self.runcmd(['gprbuild', '-q', '-P', 'main'])
            if rc != 0:
                return self.fail("gprbuild exited with status {0}", rc)

            rc = self.runcmd(["./main"] + magics['args'])
            return self.fail("executable exited with status {0}", rc)

        if rc == 0:
            self.msg("Success!", color=Fore.GREEN)

        return {'status': 'ok', 'execution_count': self.execution_count, 'payload': [], 'user_expressions': {}}
