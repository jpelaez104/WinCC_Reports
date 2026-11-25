<#
  Registra el gestor para que arranque con Windows (sin instalar software de terceros):
  una Tarea Programada "Al iniciar el sistema" que se reinicia sola si el proceso falla.

  Ejecutar en PowerShell COMO ADMINISTRADOR:
    .\scripts\instalar_tarea_inicio.ps1
  Desinstalar:
    Unregister-ScheduledTask -TaskName "GestorRegistrosWinCC" -Confirm:$false
#>
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root '.venv\Scripts\pythonw.exe'
$run = Join-Path $root 'run.py'
if (-not (Test-Path $py)) { throw "No existe $py. Cree el entorno virtual primero." }

$action = New-ScheduledTaskAction -Execute $py -Argument "`"$run`"" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest

Register-ScheduledTask -TaskName 'GestorRegistrosWinCC' -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description 'Gestor de Registros WinCC: ingesta de CSV e informes (http://127.0.0.1:8080)' -Force
Start-ScheduledTask -TaskName 'GestorRegistrosWinCC'
Write-Host 'Tarea registrada e iniciada. Interfaz: http://127.0.0.1:8080'
