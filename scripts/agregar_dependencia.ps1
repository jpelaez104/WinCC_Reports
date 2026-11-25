<#
  Agrega una o varias dependencias al proyecto y deja registrada la version instalada.

  Uso:
    .\scripts\agregar_dependencia.ps1 reportlab
    .\scripts\agregar_dependencia.ps1 "matplotlib==3.9.2" pyhanko

  Hace:
    1. pip install en el entorno virtual .venv
    2. Agrega/actualiza "paquete==version" en requirements.txt (dependencias directas)
    3. Regenera requirements-lock.txt con pip freeze (arbol completo)
#>
param([Parameter(Mandatory = $true, ValueFromRemainingArguments = $true)][string[]]$Paquetes)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { throw "No existe el entorno virtual en $root\.venv. Cree uno con: py -3.12 -m venv .venv" }

& $py -m pip install @Paquetes
if ($LASTEXITCODE -ne 0) { throw 'pip install fallo' }

$req = Join-Path $root 'requirements.txt'
$lines = [System.Collections.Generic.List[string]](Get-Content $req -Encoding utf8)

foreach ($p in $Paquetes) {
    $name = ($p -split '[=<>!~\[ ]')[0]
    $ver = (& $py -m pip show $name | Select-String '^Version:\s*(.+)$').Matches[0].Groups[1].Value
    $pattern = '^' + [regex]::Escape($name) + '\s*(==|>=|<=|~=|$)'
    $idx = -1
    for ($i = 0; $i -lt $lines.Count; $i++) { if ($lines[$i] -match $pattern -or $lines[$i].ToLower() -match ('^' + [regex]::Escape($name.ToLower()) + '==')) { $idx = $i; break } }
    $entry = "$name==$ver"
    if ($idx -ge 0) { $lines[$idx] = $entry } else {
        if (-not ($lines -contains '# --- Agregadas con agregar_dependencia.ps1 ---')) { $lines.Add(''); $lines.Add('# --- Agregadas con agregar_dependencia.ps1 ---') }
        $lines.Add($entry)
    }
    Write-Host "Registrado: $entry"
}
Set-Content -Path $req -Value $lines -Encoding utf8

$lock = Join-Path $root 'requirements-lock.txt'
$header = @('# Arbol COMPLETO de dependencias (pip freeze). Generado automaticamente por scripts\agregar_dependencia.ps1', "# Python: $(& $py --version)", "# Fecha: $(Get-Date -Format 'yyyy-MM-dd HH:mm')")
$freeze = & $py -m pip freeze
Set-Content -Path $lock -Value ($header + $freeze) -Encoding utf8
Write-Host 'requirements-lock.txt actualizado.'
