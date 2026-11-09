"""
Command Executor Module

Responsible for executing shell commands and returning results
"""

import asyncio
import os
import shlex
import signal
from typing import Dict, List, Optional, Sequence, Tuple


class ExecutionResult:
    """Command execution result"""

    def __init__(self, exit_code: int, stdout: str, stderr: str, execution_time: float):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.execution_time = execution_time

    def to_dict(self) -> Dict:
        """Convert to dictionary format"""
        return {
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "execution_time": self.execution_time,
        }


def build_shell_command(command: str, args: Optional[Sequence[str]] = None) -> str:
    """
    Build a shell command string with safely quoted arguments.

    Args:
        command: Base command
        args: Optional argument list

    Returns:
        str: Shell command string
    """
    if not args:
        return command
    quoted_args = [shlex.quote(str(arg)) for arg in args]
    return f"{command} {' '.join(quoted_args)}"


def normalize_command_and_args(command: str, args: Optional[Sequence[str]] = None) -> Tuple[str, List[str]]:
    """
    Normalize command inputs for single-string command lines.

    Args:
        command: Command string from tool input
        args: Optional argument list

    Returns:
        Tuple[str, List[str]]: Normalized command and args
    """
    normalized_command = (command or "").strip()
    normalized_args = [str(arg) for arg in args] if args else []

    if normalized_args or not normalized_command:
        return normalized_command, normalized_args

    parts = shlex.split(normalized_command, posix=True)
    if not parts:
        return "", []

    return parts[0], parts[1:]


class CommandExecutor:
    """Command executor"""

    def __init__(self, session_id: str, working_dir: Optional[str] = None, timeout: float = 300.0):
        """
        Initialize command executor

        Args:
            session_id: Chat session ID (required)
            working_dir: Working directory, if None uses ~/.castrel/${session_id}/ as default
            timeout: Command execution timeout (seconds)
        """
        self.session_id = session_id

        # Set session directory
        home_dir = os.path.expanduser("~")
        self.session_dir = os.path.join(home_dir, ".castrel", session_id)

        # Create session directory
        os.makedirs(self.session_dir, exist_ok=True)

        # Set working directory (use session directory if not specified)
        self.working_dir = working_dir if working_dir else self.session_dir
        self.timeout = timeout

        # Log file path
        self.log_file = os.path.join(self.session_dir, "terminal.log")

        # Running process, exposed so a cancellation can terminate the command.
        self._process: Optional[asyncio.subprocess.Process] = None
        self._cancelled = False

    def cancel(self) -> bool:
        """Terminate the running command, if any.

        The awaiting ``execute`` call then returns with the killed process exit code.
        A cancellation that arrives before the subprocess exists is remembered and applied
        as soon as it is spawned. Returns True when a live process was signalled.
        """
        self._cancelled = True
        process = self._process
        if process is None or process.returncode is not None:
            return False
        return self._terminate(process)

    @staticmethod
    def _terminate(process: asyncio.subprocess.Process) -> bool:
        """Kill the command and the process group it spawned, so children do not survive."""
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                process.kill()
            except ProcessLookupError:
                return False
        return True

    async def execute(self, command: str, cwd: Optional[str] = None) -> ExecutionResult:
        """
        Execute shell command asynchronously

        Args:
            command: Command to execute
            cwd: Working directory (override mode), if specified temporarily uses this directory,
                 otherwise uses default working directory

        Returns:
            ExecutionResult: Command execution result
        """
        import time

        start_time = time.time()

        # Determine actual working directory to use
        actual_cwd = cwd if cwd else self.working_dir

        try:
            # Expand ~ and environment variables in path
            working_dir = os.path.expanduser(os.path.expandvars(actual_cwd))

            # Create subprocess to execute command
            # Set stdin to DEVNULL to prevent command from waiting for input and hanging.
            # A dedicated session lets a cancellation kill the whole command process group.
            process = await asyncio.create_subprocess_shell(
                command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=working_dir,
                env=os.environ.copy(),
                start_new_session=True,
            )

            # Wait for command to complete (with timeout)
            self._process = process
            if self._cancelled:
                self._terminate(process)
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=self.timeout)
            except asyncio.TimeoutError:
                # Timeout, terminate process
                self._terminate(process)
                await process.wait()
                execution_time = time.time() - start_time
                result = ExecutionResult(
                    exit_code=-1,
                    stdout="",
                    stderr=f"Command execution timeout (exceeded {self.timeout} seconds)",
                    execution_time=execution_time,
                )
                # Log result
                self._log_command(command, working_dir, result)
                return result
            finally:
                self._process = None

            # Decode output
            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")
            exit_code = process.returncode

            execution_time = time.time() - start_time

            result = ExecutionResult(exit_code=exit_code, stdout=stdout, stderr=stderr, execution_time=execution_time)

            # Log result
            self._log_command(command, working_dir, result)

            return result

        except Exception as e:
            execution_time = time.time() - start_time
            result = ExecutionResult(
                exit_code=-2, stdout="", stderr=f"Command execution exception: {str(e)}", execution_time=execution_time
            )
            # Log result
            self._log_command(command, actual_cwd, result)
            return result

    def _log_command(self, command: str, cwd: str, result: ExecutionResult):
        """
        Log command execution to terminal.log

        Args:
            command: Executed command
            cwd: Working directory
            result: Execution result
        """
        from datetime import datetime

        try:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            log_entry = f"""[{timestamp}] COMMAND: {command}
  CWD: {cwd}
  EXIT_CODE: {result.exit_code}
  DURATION: {result.execution_time:.2f}s
  STDOUT:
    {result.stdout if result.stdout else "(empty)"}
  STDERR:
    {result.stderr if result.stderr else "(empty)"}
---

"""

            # Append to log file
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(log_entry)

        except Exception as e:
            # Logging failure should not affect command execution
            print(f"Failed to log command: {e}")
