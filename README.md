# iTOF Online Display

中文部署说明：[部署说明.md](部署说明.md)

This directory is a self-contained iTOF real-time unpacking and online display demo. Detector maps, calibration data, geometry, monitor code, and the web control page are kept inside the project. It is intended to run directly on a native Ubuntu installation and does not depend on WSL or a fixed username.

## Requirements

- Ubuntu or another compatible Linux distribution
- Python 3.6 or newer (standard library only)
- CERN ROOT with HTTP and OpenGL support
- A C++ compiler compatible with the installed ROOT build

The current development environment uses Python 3.6.9, ROOT 6.26/04, and GCC 7.5. Newer compatible versions should also work. Ports 8088 (control page) and 8090 (ROOT HTTP server) must be available.

## Start

If `root` is already available in `PATH`:

```bash
./itof/online/restart_control_demo.sh 8088
```

If ROOT is not initialized, point the launcher at its environment script:

```bash
export ROOT_SETUP=/path/to/root/bin/thisroot.sh
./itof/online/restart_control_demo.sh 8088
```

Open `http://127.0.0.1:8088/`. The scripts derive the project directory from their own location, so the whole directory may be copied to another path or computer without editing source files.

The service runs as the user who launches it. PID files, logs, `$HOME`, and ROOT discovery therefore follow the account on the destination computer; no `zhou` account or `/home/zhou` path is required. Saved project-internal paths from the original computer are relocated automatically when their corresponding resource exists in the copied project.

## Project Resources

- `itof/data/`: monitored data directories
- `itof/reco/map/`: electronics maps
- `itof/macro/newchip/Calib_iTOF.root`: file-mode iTOF calibration
- `itof/macro/newchip/CLSB.txt`: fine-time calibration data
- `itof/geo/itof_geomanager_vbatch2409.root`: detector geometry
- `itof/online/`: ROOT monitor, control server, and launch scripts
- `itof/online/logs/`: local logs and saved web settings

The default `self` calibration mode does not require `Calib_iTOF.root`. Select file calibration in the web page when the bundled calibration should be applied.
