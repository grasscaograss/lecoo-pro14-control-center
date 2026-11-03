# Notice

This project is based on [LaVashikk/Lecoo-Control-Center](https://github.com/LaVashikk/Lecoo-Control-Center).

The original project is licensed under the MIT License.

This repository contains modified GUI code, Windows helper scripts, fan curve handling, service repair/recovery behavior, and packaging configuration for Lecoo Pro14 usage.

The current runtime is based on upstream 0.3.3, commit `dd850af1f15110deed00b9e3b7546b5fa8b3ea41`. The local patch pins `interprocess` to 2.4.2 and adds Windows pipe-disconnection regression tests. The core and IPC protocol remain 0.3.3. Interprocess is licensed under 0BSD.

Binary builds and packaged test archives are distributed through GitHub Releases instead of being tracked in the source repository.
