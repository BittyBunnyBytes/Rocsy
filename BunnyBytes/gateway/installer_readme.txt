ROCSY INSTALLER

WINDOWS (UNTESTED): run "Install Rocsy (Windows).cmd" instead of the steps below.
   It detects Windows 7, 8, 8.1, 10 and 11. On Windows 11, and Windows 10 version
   2004 (build 19041) or newer, it sets up WSL2 + Ubuntu, systemd and Docker Engine
   there, then runs this installer inside WSL2 and adds Rocsy shortcuts to the
   Windows desktop and Start menu. Windows 7, 8, 8.1 and older Windows 10 can't run
   WSL2 or Docker: it offers a shortcut to a Rocsy server on your network instead.
   For the GPU: the current AMD Windows driver, plus AMD's ROCm for WSL in Ubuntu.
   Windows support has not been tested on real hardware yet; keep install.log
   (in the Rocsy folder) for bug reports.

1. Extract this tar.gz archive with your archive manager.
2. Open "Install Rocsy.desktop" from the extracted folder. Your desktop may ask
   you to allow launching this downloaded file.
3. If desktop launching is unavailable, run:
      sh "Install Rocsy.sh"
   To install from the terminal instead of the browser:
      sh "Install Rocsy.sh" --terminal    the same questions, asked in the terminal
      sh "Install Rocsy.sh" --defaults    the default install, no questions
      sh "Install Rocsy.sh" --help        all options (--dir PATH, --verbose)
   In the terminal, press Enter for the default install or type c to choose the
   options. The default install puts Rocsy in the default folder with Motion,
   ComfyUI, Hermes Agent and yt-dlp (REA and the Trainer are left out), and
   prints a generated password at the end. Without a desktop session (SSH) the
   installer starts in the terminal by itself.
4. Choose the install location and select "Install and build Rocsy". The installer
   downloads missing Docker packages on Arch Linux and Ubuntu-based systems,
   starts Docker, grants your account Docker access, then builds and starts
   Rocsy automatically. Administrator authentication may be requested.
   If the installer adds your account to the Docker group, sign out and back
   in before running docker compose without sudo in a terminal. The installer
   can finish in the current session; a computer restart is not needed.
   On CachyOS or Arch, if package URLs return 404, enable the installer option
   to refresh mirrors and perform a full system update before installing Docker.
   A full update may change packages beyond Rocsy and may require a reboot.

If the selected folder contains a Rocsy installation, choose Update, Repair, or
Complete reinstall. Update and Repair keep saved data and the current password.
Complete reinstall deletes the installation folder and Rocsy Docker volumes;
it requires the confirmation phrase. The current password is required only if
you set one yourself. Leave it blank if you never set one.
Fresh installs and complete reinstalls use a password you choose, or can run
without one after you acknowledge the security warning.

The installer starts the web services for Video, Image, Music, Voice, Agent,
Models, Llama and Connections. "What to install" picks the rest:
  Motion                on by default: the Master Agent's video renderer
  REA                   off by default: game modding tools (experimental)
  Trainer               off by default: LoRA training (20+ GB image)
  ComfyUI               on: downloads ComfyUI + PyTorch for ROCm into the Rocsy
                        folder (or uses the folder you enter) and sets up the
                        supervisor that starts it on demand
  Hermes Agent          on: Personal Hermes (the hermes command) + Rocsy's copy;
                        the Master Agent and Agentic modes need it
  yt-dlp                on: web video references for the agent
Backends are set up after Rocsy starts; one that fails is listed at the end
and can be retried in Models -> Manager -> Core Backend, like anything skipped.
Pages can open before models are downloaded: Llama needs an LLM, the studios
need their model files, and Connections needs credentials.

Terminal and desktop (installer toggles, on by default):
  rocsy                 chat with the Master Agent in the terminal
  rocsy chat -q "..."   one message, print the reply, exit
  rocsy desktop         Rocsy Desktop: Rocsy in its own window (also in the app menu)
  rocsy up / down / status / logs
The command is ~/.local/bin/rocsy; Rocsy Desktop uses Chromium, Chrome, Brave,
Edge or Vivaldi for its window, otherwise your default browser. Uninstalling
Rocsy removes both; unchecking one during an Update removes just that one.
The host services helper (also on by default) powers Settings -> Services,
GPU tuning and Core Backend; with it off, only uninstalling works there.

The launcher installs Python 3.9 or newer when needed on supported systems.
Docker group access grants root-level control of the machine. On other Linux
distributions, install Python 3.9+, Docker Engine, Docker Compose and Buildx
first. Models, LoRAs and installed trainers are not bundled; they download later.
