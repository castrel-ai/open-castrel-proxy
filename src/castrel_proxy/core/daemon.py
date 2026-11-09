"""
Daemon Process Management Module

Handles background process management, PID files, and logging
"""

import atexit
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class DaemonManager:
    """Daemon process manager"""

    def __init__(self, pid_file: Path, log_file: Path):
        """
        Initialize daemon manager

        Args:
            pid_file: PID file path
            log_file: Log file path
        """
        self.pid_file = pid_file
        self.log_file = log_file

    def daemonize(self):
        """
        Daemonize the current process (Unix double-fork)

        Raises:
            OSError: If forking fails
        """
        # Check if already running
        if self.is_running():
            pid = self.get_pid()
            raise RuntimeError(f"Daemon already running with PID {pid}")

        # Ensure parent directories exist
        self.pid_file.parent.mkdir(parents=True, exist_ok=True)
        self.log_file.parent.mkdir(parents=True, exist_ok=True)

        # First fork
        try:
            pid = os.fork()
            if pid > 0:
                # Parent process exits
                sys.exit(0)
        except OSError as e:
            logger.error(f"First fork failed: {e}")
            sys.exit(1)

        # Decouple from parent environment
        os.chdir("/")
        os.setsid()
        os.umask(0)

        # Second fork
        try:
            pid = os.fork()
            if pid > 0:
                # Parent process exits
                sys.exit(0)
        except OSError as e:
            logger.error(f"Second fork failed: {e}")
            sys.exit(1)

        # Redirect standard file descriptors
        sys.stdout.flush()
        sys.stderr.flush()

        # Open log file
        log_fd = os.open(str(self.log_file), os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o644)

        # Redirect stdin to /dev/null
        with open(os.devnull, "r") as devnull:
            os.dup2(devnull.fileno(), sys.stdin.fileno())

        # Redirect stdout and stderr to log file
        os.dup2(log_fd, sys.stdout.fileno())
        os.dup2(log_fd, sys.stderr.fileno())

        os.close(log_fd)

        # Write PID file
        self._write_pid()

        # Register cleanup on exit
        atexit.register(self._cleanup)

        # Handle termination signals
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

    def register_current_process(self) -> None:
        """Track a foreground process with the same pid file used by background runs."""
        if self.is_running():
            raise RuntimeError(f"Proxy node is already running with PID {self.get_pid()}")
        self.pid_file.parent.mkdir(parents=True, exist_ok=True)
        self._write_pid()
        atexit.register(self._cleanup)

    def _write_pid(self):
        """Write current process PID to file"""
        pid = os.getpid()
        with open(self.pid_file, "w") as f:
            f.write(str(pid))
        logger.info(f"Daemon started with PID {pid}")

    def _cleanup(self):
        """Cleanup PID file on exit"""
        if self.pid_file.exists():
            self.pid_file.unlink()
            logger.info("Daemon stopped, PID file removed")

    def _signal_handler(self, signum, frame):
        """Handle termination signals"""
        logger.info(f"Received signal {signum}, shutting down...")
        sys.exit(0)

    def get_pid(self) -> Optional[int]:
        """
        Get PID from PID file

        Returns:
            Optional[int]: PID if file exists and is valid, None otherwise
        """
        if not self.pid_file.exists():
            return None

        try:
            with open(self.pid_file, "r") as f:
                pid = int(f.read().strip())
            return pid
        except (ValueError, IOError):
            return None

    def is_running(self) -> bool:
        """
        Check if daemon process is running

        Returns:
            bool: True if running, False otherwise
        """
        pid = self.get_pid()
        if pid is None:
            return False

        try:
            # Check if process exists (send signal 0)
            os.kill(pid, 0)
            return True
        except OSError:
            # Process doesn't exist, clean up stale PID file
            self.pid_file.unlink()
            return False

    def stop(self) -> bool:
        """
        Stop daemon process

        Returns:
            bool: True if stopped successfully, False if not running
        """
        pid = self.get_pid()
        if pid is None:
            return False

        if not self.is_running():
            # Clean up stale PID file
            if self.pid_file.exists():
                self.pid_file.unlink()
            return False

        try:
            # Send SIGTERM to gracefully terminate
            os.kill(pid, signal.SIGTERM)

            # Wait for process to exit (with timeout)
            import time

            for _ in range(50):  # Wait up to 5 seconds
                time.sleep(0.1)
                if not self.is_running():
                    return True

            # If still running, force kill
            if self.is_running():
                os.kill(pid, signal.SIGKILL)
                time.sleep(0.5)

            return True
        except OSError as e:
            logger.error(f"Failed to stop daemon: {e}")
            return False


def get_daemon_manager() -> DaemonManager:
    """
    Get daemon manager instance

    Returns:
        DaemonManager: Daemon manager with default paths
    """
    config_dir = Path.home() / ".castrel"
    pid_file = config_dir / "castrel-proxy.pid"
    log_file = config_dir / "castrel-proxy.log"

    return DaemonManager(pid_file, log_file)


def get_proxy_daemon_manager() -> DaemonManager:
    """Daemon manager for `castrel-proxy proxy start` (separate pid/log files)."""
    config_dir = Path.home() / ".castrel"
    return DaemonManager(config_dir / "proxy-node.pid", config_dir / "proxy-node.log")
