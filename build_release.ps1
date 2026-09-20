$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot
$source = [IO.File]::ReadAllText((Join-Path $projectRoot "ksa_macro_main.py"), [Text.Encoding]::UTF8)
$version = [regex]::Match($source, 'VERSION = "([^"]+)"').Groups[1].Value
if (-not $version) { throw "ksa_macro_main.py에서 VERSION을 찾지 못했습니다." }

Push-Location $projectRoot
try {
    python -m unittest discover -s tests -v
    if ($LASTEXITCODE) { throw "테스트가 실패했습니다." }

    python -m PyInstaller --log-level WARN --noconfirm --clean --distpath build\release --workpath build\release-work KsaMacro.spec
    if ($LASTEXITCODE) { throw "PyInstaller 빌드가 실패했습니다." }

    $guidePath = (Get-ChildItem -LiteralPath (Join-Path $projectRoot "dist") -Filter *.html).FullName
    $guide = [IO.File]::ReadAllText($guidePath, [Text.Encoding]::UTF8)
    $guide = [regex]::Replace($guide, 'v\d+\.\d+\.\d+', "v$version")
    [IO.File]::WriteAllText($guidePath, $guide, (New-Object Text.UTF8Encoding($false)))

    Copy-Item build\release\KsaMacro.exe dist\bin\KsaMacro_core.dat -Force
    $archive = Join-Path $projectRoot "KsaMacro-v$version-windows.zip"
    Compress-Archive -Path dist\* -DestinationPath $archive -Force
    Write-Host "완료: $archive"
}
finally {
    Pop-Location
}
