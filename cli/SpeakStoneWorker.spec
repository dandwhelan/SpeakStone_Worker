# PyInstaller spec. Build (from cli/): .venv\Scripts\pyinstaller SpeakStoneWorker.spec
# One-file console exe; the three worker scripts are bundled under worker/, the tray icon under assets/.
import sys
from pathlib import Path
root = Path(SPECPATH).parent
assets = Path(SPECPATH) / 'speakstone_worker' / 'assets'
# the tray library picks its backend at run time, so the backend modules are named here
hidden = (['pystray._win32'] if sys.platform == 'win32' else ['pystray._xorg']) + ['certifi']   # its hook bundles the certificate list
a = Analysis(['run_cli.py'], pathex=[SPECPATH],
             datas=[(str(root / n), 'worker') for n in ('net_worker.py', 'indextts2_worker.py', 'runpod_worker.py')]
                   + [(str(assets / n), 'assets') for n in ('icon.png', 'icon.ico')],
             hiddenimports=hidden,
             excludes=['numpy', 'torch', 'matplotlib', 'scipy', 'tkinter'])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='SpeakStoneWorker', console=True, upx=False,
          icon=str(assets / 'icon.ico'))
