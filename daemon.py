from daemon.main import main
from crash_reporter import install as _install_crash
_install_crash()

if __name__ == "__main__":
    main()
