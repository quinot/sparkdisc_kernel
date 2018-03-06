# Adapted from https://github.com/sourceryinstitute/jupyter-CAF-kernel

from colorama import Fore, Style
from queue import Queue
from threading import Thread

from ipykernel.kernelbase import Kernel
import re
import subprocess
import tempfile
import os
from shlex import split as shsplit

msg_prefix = "[SPARK Discovery kernel] "
gnatprove_error_re = re.compile(b"([a-zA-Z._-]+):(\d+):(\d+):(.+)")


class RealTimeSubprocess(subprocess.Popen):
    """
    A subprocess that allows to read its stdout and stderr in real time
    """

    def __init__(self, cmd, write_to_stdout, write_to_stderr, line_filter):
        """
        :param cmd: the command to execute
        :param write_to_stdout: function called with chunks of data from stdout
        :param write_to_stderr: function called with chunks of data from stderr
        :param function|None line_filter:
            if present, function to transform each stdout line (takes bytes and
            returns bytes or None)
        """
        self._write = {"out": write_to_stdout, "err": write_to_stderr}
        self._line_filter = line_filter

        super(RealTimeSubprocess, self).__init__(cmd,
                                                 stdout=subprocess.PIPE,
                                                 stderr=subprocess.PIPE,
                                                 bufsize=1,  # Line buffered
                                                 close_fds=True)

        self._queue = Queue()
        self._threads = {}

        for name, stream in (("out", self.stdout), ("err", self.stderr)):
            thread = Thread(target=RealTimeSubprocess._enqueue_output,
                            args=(stream, self._queue, name))
            thread.daemon = True
            thread.start()
            self._threads[name] = thread

    @staticmethod
    def _enqueue_output(stream, queue, what):
        """
        Add chunks of data from a stream to a queue until the stream is empty.
        """
        for line in iter(stream.readline, b""):
            queue.put((what, line))

        # Signal main thread that this output stream is closed

        queue.put((what, None))

    def write_contents(self):
        """
        Write the available content from stdin and stderr where specified when the instance was created

        This blocks until data is available on the queue. This terminates when both queueing threads
        have signalled that their respective streams are closed.

        :return:
        """

        while True:
            what, line = self._queue.get()
            if line is None:
                self._threads[what].join()

            else:
                if self._line_filter is not None:
                    line = self._line_filter(line)
                if line is not None:
                    self._write[what](line)

            # Return if queue is empty and both threads are dead

            if self._queue.empty() and all(not t.is_alive() for t in self._threads.values()):
                break

class SPARKDiscKernel(Kernel):
    implementation = "sparkdisc_kernel"
    implementation_version = "0.1"
    language = "SPARK"
    language_version = "SPARK 2014"
    language_info = {"name": "spark",
                     "mimetype": "text/plain",
                     "file_extension": ".ada",
                     "codemirror_mode": "ada",
                     "pygments_lexer": "ada"}
    banner = "SPARK Discovery kernel.\n" \
             "Creates source code files and executables in a temporary folder.\n"

    def __init__(self, *args, **kwargs):
        super(SPARKDiscKernel, self).__init__(*args, **kwargs)
        self.tmpdir = tempfile.TemporaryDirectory()

    def _write_to_stdout(self, contents):
        self.send_response(self.iopub_socket, 'stream', {'name': 'stdout', 'text': contents})

    def _write_to_stderr(self, contents):
        self.send_response(self.iopub_socket, 'stream', {'name': 'stderr', 'text': contents})

    def create_jupyter_subprocess(self, cmd, line_filter):
        return RealTimeSubprocess(cmd,
                                  lambda contents: self._write_to_stdout(contents.decode()),
                                  lambda contents: self._write_to_stderr(contents.decode()),
                                  line_filter)

    def runcmd(self, cmd, line_filter=None):
        p = self.create_jupyter_subprocess(cmd, line_filter)
        p.write_contents()

        # Here when both stdout and stderr have been closed: collect
        # child process's return code.

        p.wait()
        return p.returncode

    def _filter_magics(self, code):

        magics = {'cflags': [],
                  'ldflags': [],
                  'args': []}

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
        color = kwargs.pop("color", None)
        self._write_to_stdout((color if color else "") +
                              msg_prefix +
                              msg.format(*args, **kwargs) +
                              (Style.RESET_ALL if color else "") +
                              "\n")

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

        error_count = [0]
        # Note: This is a list, not a simple int variable, because we
        # need it to be a mutable object so that we can modify the
        # value from within the gnatprove_filter function below.

        def gnatprove_filter(line):
            """Filter a line of gnatprove output

            Tally errors in error_count[0]; suppress "Summary logged" line.

            :param bytes line: line of intput
            :rtype: bytes|None
            """

            if line.startswith(b"Summary logged"):
                return None

            if gnatprove_error_re.match(line):
                error_count[0] += 1
                line = bytes(Fore.RED, "ascii") + line

            return line

        rc = self.runcmd(['gnatprove', '-P', 'main'], line_filter = gnatprove_filter)
        if rc != 0:
            return self.fail("gnatprove exited with status {0}", rc)

        if error_count[0] == 0:
            self.msg("Success!", color=Fore.GREEN)
        else:
            self.msg("{0} error{s}", error_count[0], s="s" if error_count[0] > 1 else "", color=Fore.RED)
            return self.fail("gnatprove failed")

        # Call builder and run program

        main_unit = self.metadata.get("main")
        if main_unit is not None:
            rc = self.runcmd(['gprbuild', '-q', '-P', 'main', main_unit])
            if rc != 0:
                return self.fail("gprbuild exited with status {0}", rc)

            rc = self.runcmd([os.path.join(".", main_unit)] + magics['args'])
            return self.fail("executable exited with status {0}", rc)

        return {'status': 'ok', 'execution_count': self.execution_count,
                'payload': [], 'user_expressions': {}}
