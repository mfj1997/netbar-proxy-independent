$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$buildRoot = Join-Path $projectRoot '.build'
$pythonExe = Join-Path $buildRoot 'venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) {
    python -m venv (Join-Path $buildRoot 'venv')
    if ($LASTEXITCODE -ne 0) { throw '创建构建环境失败' }
}
& $pythonExe -m pip install -r (Join-Path $PSScriptRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw '安装依赖失败' }
$pythonBase = (& $pythonExe -c 'import sys; print(sys.base_prefix)').Trim()
$savedBuildPath = $env:PATH
try {
    # Prevent unrelated media tools on PATH from supplying incompatible ICU/CRT DLLs.
    $env:PATH = @(
        (Split-Path -Parent $pythonExe),
        $pythonBase,
        (Join-Path $pythonBase 'DLLs'),
        (Join-Path $env:SystemRoot 'System32'),
        $env:SystemRoot
    ) -join ';'
    & $pythonExe -m PyInstaller --clean --noconfirm --onefile --windowed --name '网吧代理独立版' --distpath $projectRoot --workpath (Join-Path $buildRoot 'pyinstaller') --specpath $buildRoot (Join-Path $PSScriptRoot 'app.py')
    if ($LASTEXITCODE -ne 0) { throw '打包失败' }
} finally {
    $env:PATH = $savedBuildPath
}
Write-Output (Join-Path $projectRoot '网吧代理独立版.exe')
