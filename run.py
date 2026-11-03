"""Start the DusterScan desktop app."""

import sys

if __name__ == "__main__":
    args = sys.argv[1:]
    if "--apply-update" in args:
        # A new version, started from its downloaded copy to replace the installed one (no window).
        from dusterscan.updater import apply_main
        sys.exit(apply_main(args))
    from dusterscan.app import main, self_check
    if "--self-check" in args:
        sys.exit(self_check(args))
    main(args)
