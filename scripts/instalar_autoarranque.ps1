# Registra (o reemplaza) la tarea programada que al iniciar sesion levanta
# el bot de Telegram y la webapp en vivo, ocultos. No necesita administrador.
# Probar ahora:   schtasks /run /tn "ZEIA-IA autoarranque"
# Quitar:         schtasks /delete /tn "ZEIA-IA autoarranque" /f
$ErrorActionPreference = 'Stop'

$tarea = 'ZEIA-IA autoarranque'
$vbs = Join-Path $PSScriptRoot 'autoarranque.vbs'
if (-not (Test-Path $vbs)) { throw "No se encontro $vbs" }

$accion = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument ('"' + $vbs + '"')
$disparador = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$disparador.Delay = 'PT30S'
$ajustes = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $tarea -Action $accion -Trigger $disparador `
    -Settings $ajustes -Force | Out-Null

Write-Host "Tarea '$tarea' registrada: al iniciar sesion (30 s despues) arranca"
Write-Host "el bot y la webapp ocultos. Log: logs\autoarranque.log"
