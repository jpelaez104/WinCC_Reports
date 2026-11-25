<#
  Ejecuta la bateria de pruebas con cobertura y guarda la evidencia de la ejecucion.

  Uso:
    .\scripts\probar.ps1            # todas las pruebas (incluida la de volumen de 3 meses)
    .\scripts\probar.ps1 -Rapido    # sin las pruebas marcadas 'lento'

  Deja en reportes_pruebas\:
    resultados_<fecha>.xml   resultado de cada prueba (JUnit XML, archivable como evidencia)
    cobertura\index.html     lineas de app\ que ejecuta cada prueba (rojo = sin probar)
    resumen_<fecha>.txt      salida completa de pytest con la tabla de cobertura

  Falla (codigo de salida distinto de 0) si alguna prueba falla o si la cobertura de los
  modulos criticos (ingest, wincc_csv, reports, report_xlsx, auth) baja del 90 %.
#>
param([switch]$Rapido)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root '.venv\Scripts\python.exe'
Set-Location $root
$out = Join-Path $root 'reportes_pruebas'
New-Item -ItemType Directory -Force $out | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$junit = Join-Path $out "resultados_$stamp.xml"
$resumen = Join-Path $out "resumen_$stamp.txt"

$pargs = @('-m', 'pytest', '-q', '--cov', '--cov-report=term-missing', '--cov-report=html', "--junitxml=$junit")
if ($Rapido) { $pargs += @('-m', 'not lento') }

& $py @pargs 2>&1 | Tee-Object -FilePath $resumen
$codigo = $LASTEXITCODE

# umbral de cobertura: CADA modulo critico por separado debe llegar al 90 %
$codigoCob = 0
foreach ($m in 'ingest', 'wincc_csv', 'reports', 'report_xlsx', 'auth') {
    & $py -m coverage report --include="app/$m.py" --fail-under=90 | Select-Object -Last 1 |
        Tee-Object -FilePath $resumen -Append
    if ($LASTEXITCODE -ne 0) { Write-Host "  app\$m.py por debajo del 90 %" -ForegroundColor Red; $codigoCob = 2 }
}

Write-Host ''
Write-Host "Evidencia: $junit"
Write-Host "Cobertura: $(Join-Path $out 'cobertura\index.html')"
if ($codigo -ne 0) { Write-Host 'RESULTADO: HAY PRUEBAS EN ROJO' -ForegroundColor Red; exit $codigo }
if ($codigoCob -ne 0) { Write-Host 'RESULTADO: cobertura de modulos criticos < 90 %' -ForegroundColor Red; exit 2 }
Write-Host 'RESULTADO: TODO EN VERDE' -ForegroundColor Green
