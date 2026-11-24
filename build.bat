@echo off
REM Genera dist\BuscadorNovelas\ (modo carpeta) y dist\BuscadorNovelas.zip. Necesita Python 3.9+ con "Add Python to PATH".
python -m pip install -r requirements.txt pyinstaller
python -m PyInstaller --noconfirm --clean BuscadorNovelas.spec
powershell -NoProfile -Command "Compress-Archive -Path 'dist\BuscadorNovelas' -DestinationPath 'dist\BuscadorNovelas.zip' -Force"
echo.
echo Listo:
echo    dist\BuscadorNovelas\BuscadorNovelas.exe   (para usar)
echo    dist\BuscadorNovelas.zip                   (para publicar)
echo.
echo Huella SHA-256 del zip (publicala junto al enlace del analisis de VirusTotal):
certutil -hashfile dist\BuscadorNovelas.zip SHA256
pause
