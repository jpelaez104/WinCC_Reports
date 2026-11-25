<#
  Descarga ECharts a static\vendor para que la interfaz funcione SIN internet en el PC de planta.
  Ejecutar una vez en un equipo con internet y copiar la carpeta static\vendor al PC de WinCC.
#>
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$dest = Join-Path $root 'static\vendor'
New-Item -ItemType Directory -Force $dest | Out-Null
$ver = '5.5.1'
Invoke-WebRequest "https://cdn.jsdelivr.net/npm/echarts@$ver/dist/echarts.min.js" -OutFile (Join-Path $dest 'echarts.min.js') -UseBasicParsing
Set-Content (Join-Path $dest 'VERSIONS.txt') "echarts==$ver  (https://cdn.jsdelivr.net/npm/echarts@$ver/dist/echarts.min.js)" -Encoding utf8
Write-Host "ECharts $ver guardado en $dest"
