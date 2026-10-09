# Prepara la version portatil del Monitor Al Aire:
#  - Python portatil (en .\python)
#  - Paquetes de Python (requirements.txt)
#  - ffmpeg (en .\ffmpeg)
# Solo descarga lo que falte; si todo esta listo termina en segundos.

# "Continue": en PowerShell 5 los mensajes de error de programas externos
# no deben detener el script; cada paso revisa su resultado.
$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$root   = Split-Path -Parent $PSScriptRoot
$app    = $PSScriptRoot
$pyDir  = Join-Path $root "python"
$py     = Join-Path $pyDir "python.exe"
$ffDir  = Join-Path $root "ffmpeg"
$ff     = Join-Path $ffDir "ffmpeg.exe"
$tmp    = Join-Path $root "descargas_tmp"
$pyVer  = "3.12.10"

function Paso($t)  { Write-Host "  > $t" -ForegroundColor Cyan }
function Ok($t)    { Write-Host "    $t" -ForegroundColor Green }
function Falla($t) { Write-Host "`n  ERROR: $t`n" -ForegroundColor Red; exit 1 }

function Descargar($url, $dest) {
    for ($i = 1; $i -le 3; $i++) {
        try { Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing -ErrorAction Stop; return $true }
        catch { Start-Sleep -Seconds (2 * $i) }
    }
    return $false
}

Write-Host ""
Write-Host "  Monitor Al Aire - revisando componentes" -ForegroundColor White
Write-Host ""
New-Item -ItemType Directory -Force -Path $tmp | Out-Null

# ---------- 1. Python portatil ----------
Paso "Python"
if (-not (Test-Path $py)) {
    Write-Host "    Descargando Python $pyVer (solo la primera vez)..."
    $zip = Join-Path $tmp "python.zip"
    if (-not (Descargar "https://www.python.org/ftp/python/$pyVer/python-$pyVer-embed-amd64.zip" $zip)) {
        Falla "No se pudo descargar Python. Revisa la conexion a internet."
    }
    try { Expand-Archive -Path $zip -DestinationPath $pyDir -Force -ErrorAction Stop }
    catch { Remove-Item $pyDir -Recurse -Force -ErrorAction SilentlyContinue; Falla "No se pudo descomprimir Python." }
    # Habilitar site-packages (necesario para pip)
    $pth = Get-ChildItem $pyDir -Filter "python*._pth" | Select-Object -First 1
    (Get-Content $pth.FullName) -replace '^#\s*import site', 'import site' | Set-Content $pth.FullName -Encoding ASCII
}
Ok "Listo"

# ---------- 2. pip ----------
Paso "Instalador de paquetes (pip)"
& $py -m pip --version *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "    Descargando pip..."
    $gp = Join-Path $tmp "get-pip.py"
    if (-not (Descargar "https://bootstrap.pypa.io/get-pip.py" $gp)) { Falla "No se pudo descargar pip." }
    & $py $gp --no-warn-script-location -q
    if ($LASTEXITCODE -ne 0) { Falla "No se pudo instalar pip." }
}
Ok "Listo"

# ---------- 3. Paquetes ----------
Paso "Paquetes de Python"
$req = Join-Path $app "requirements.txt"
$marca = Join-Path $pyDir ".paquetes_ok"
$hash = (Get-FileHash $req -Algorithm SHA256).Hash
$necesita = $true
if ((Test-Path $marca) -and ((Get-Content $marca -Raw).Trim() -eq $hash)) {
    & $py -c "import PySide6.QtWidgets" *> $null
    if ($LASTEXITCODE -eq 0) { $necesita = $false }
}
if ($necesita) {
    Write-Host "    Instalando paquetes (puede tardar unos minutos la primera vez)..."
    & $py -m pip install --no-warn-script-location --disable-pip-version-check -q -r $req
    if ($LASTEXITCODE -ne 0) { Falla "No se pudieron instalar los paquetes." }
    & $py -c "import PySide6.QtWidgets"
    if ($LASTEXITCODE -ne 0) { Falla "Los paquetes se instalaron pero no cargan." }
    Set-Content -Path $marca -Value $hash
}
Ok "Listo"

# ---------- 4. ffmpeg ----------
Paso "ffmpeg (para medir el audio)"
if (-not (Test-Path $ff)) {
    New-Item -ItemType Directory -Force -Path $ffDir | Out-Null
    # Si ya hay un ffmpeg.exe junto al programa o en el PATH, se usa ese
    $local = @((Join-Path $root "ffmpeg.exe"), (Join-Path (Split-Path -Parent $root) "ffmpeg.exe")) |
             Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $local) { $local = (Get-Command ffmpeg.exe -ErrorAction SilentlyContinue).Source }
    if ($local) {
        Copy-Item $local $ff
    } else {
        Write-Host "    Descargando ffmpeg (solo la primera vez, ~90 MB)..."
        $zip = Join-Path $tmp "ffmpeg.zip"
        $urls = @("https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
                  "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip")
        $ok = $false
        foreach ($u in $urls) { if (Descargar $u $zip) { $ok = $true; break } }
        if (-not $ok) { Falla "No se pudo descargar ffmpeg. Revisa la conexion a internet." }
        $ext = Join-Path $tmp "ffmpeg"
        try { Expand-Archive -Path $zip -DestinationPath $ext -Force -ErrorAction Stop }
        catch { Falla "No se pudo descomprimir ffmpeg." }
        $exe = Get-ChildItem $ext -Recurse -Filter "ffmpeg.exe" | Select-Object -First 1
        if (-not $exe) { Falla "El paquete de ffmpeg no trae ffmpeg.exe." }
        Copy-Item $exe.FullName $ff
    }
}
& $ff -hide_banner -version *> $null
if ($LASTEXITCODE -ne 0) { Falla "ffmpeg no funciona en esta PC." }
Ok "Listo"

# ---------- 5. Acceso directo en el Escritorio ----------
$lnk = Join-Path ([Environment]::GetFolderPath("Desktop")) "Monitor Al Aire.lnk"
if (-not (Test-Path $lnk)) {
    try {
        $s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
        $s.TargetPath = Join-Path $root "Iniciar Monitor.bat"
        $s.WorkingDirectory = $root
        $s.IconLocation = Join-Path $app "icono.ico"
        $s.WindowStyle = 7
        $s.Save()
        Ok "Acceso directo creado en el Escritorio"
    } catch { }
}

Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
Write-Host ""
Write-Host "  Todo listo. Abriendo el monitor..." -ForegroundColor Green
exit 0
