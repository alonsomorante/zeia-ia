# Bot de Telegram en una EC2 (24/7)

Solo el bot de cobertura/lecturas: `/energia`, `/ambiental`, `/agua`,
`/avisos`, `/mantenimiento`, los avisos automáticos y la foto de cierre de las
00:10. La webapp, el chat con IA y el reporte semanal se quedan en la laptop.

## Qué se sube y qué no

| Va al servidor | No va |
|---|---|
| `scripts/telegram_bot.py`, `scripts/avisos.py` y los 12 módulos de `src/` que usa el bot | `webapp.py`, `cli.py`, `src/agent.py`, `prompts.py`, `tools.py`, voz, `web/` |
| Las pruebas de esos módulos | Facturas PDF, `backups/`, `analisis/`, `eval/`, `docs/` |
| Un `.env` solo con las variables del bot | Claves de OpenRouter y ElevenLabs |
| Las 2 llaves de los túneles (`energy.pem`, `valhallaprod.pem`) | El resto del `.env` de la laptop |

La lista exacta está en `ARCHIVOS` de `empaquetar.py`: lo que no está en la
lista no viaja.

En el servidor el bot corre como el usuario `zeiabot` (sin `sudo`, sin
shell). Solo puede escribir en `app/data/` (su estado) y en su HOME (known_hosts
de ssh, caché de Chromium); el código y el `.env` son de solo lectura para él.
No abre ningún puerto: solo sale a Telegram (443) y a los 2 bastiones (22).

## 0. La EC2

- Ubuntu 24.04, **t3.small** (2 GB), disco de 20 GB.
- Grupo de seguridad: entrada solo SSH (22) **desde tu IP**. Nada más.
- Los bastiones de las bases (`54.242.41.196` y
  `ec2-44-206-41-101.compute-1.amazonaws.com`) deben aceptar SSH desde la IP
  de la EC2. Si su grupo de seguridad está limitado por IP, agrégala. Conviene
  darle a la EC2 una **Elastic IP** para que no cambie al reiniciarla.

## 1. En la laptop: armar el paquete

```powershell
venv\Scripts\python.exe deploy\bot\empaquetar.py --env
```

Deja en `dist\`:
- `zeia-bot-<commit>.tar.gz`: el código (sin secretos).
- `bot.env`: el `.env` del servidor, con tus valores reales y solo las
  variables del bot. **Tiene secretos**: bórralo al terminar el paso 2.

## 2. Copiar al servidor

`<llave-ec2>` es la llave con la que entras a la EC2; `<ip>`, su IP.

```powershell
scp -i <llave-ec2>.pem dist\zeia-bot-*.tar.gz dist\bot.env energy.pem valhallaprod.pem ubuntu@<ip>:~/
```

## 3. En el servidor: instalar

```bash
sudo install -d -m 755 /opt/zeia-bot/app
sudo mv ~/bot.env /opt/zeia-bot/app/.env
sudo mv ~/energy.pem ~/valhallaprod.pem /opt/zeia-bot/app/
tar -xzf zeia-bot-*.tar.gz deploy/bot/instalar.sh
sudo bash deploy/bot/instalar.sh zeia-bot-*.tar.gz
```

El instalador (~5 min la primera vez): instala Python, ssh, fuentes y
Chromium; crea el usuario y las carpetas; corre las pruebas; deja permisos 600
en `.env` y las llaves; instala el servicio y corre la **verificación**, que
revisa las 2 bases por túnel, el token de Telegram (sin mandar mensajes) y que
Chromium dibuje una foto. **No enciende el bot.**

Si la verificación falla, el mensaje dice qué falta. Para repetirla:

```bash
sudo systemctl start zeia-bot-verificar; journalctl -u zeia-bot-verificar -n 30 -o cat
```

## 4. El cambio: apagar la laptop y encender el servidor

Nunca dos bots a la vez: Telegram rechaza a uno (error 409) y los avisos se
duplican.

En la laptop (PowerShell, en la carpeta del proyecto):

```powershell
# 1) que el autoarranque ya no lance el bot (la webapp sigue arrancando sola)
Set-Content data\bot_en_servidor.txt "El bot corre en la EC2. Borra este archivo para volver a correrlo aqui."
# 2) apagar el bot que está corriendo
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'telegram_bot\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId }
# 3) llevar su estado (avisos y foto de cierre) para que no repita nada
scp -i <llave-ec2>.pem data\avisos_*.sqlite data\foto_cierre.json ubuntu@<ip>:~/
```

En el servidor:

```bash
sudo mv ~/avisos_*.sqlite ~/foto_cierre.json /opt/zeia-bot/app/data/
sudo chown -R zeiabot:zeiabot /opt/zeia-bot/app/data
sudo systemctl enable --now zeia-bot
journalctl -u zeia-bot -f
```

Debe decir `Bot @zeia_alerts_bot escuchando...`. Prueba `/energia` en el grupo.
Borra `dist\bot.env` de la laptop.

## Día a día

| Para | Comando (en el servidor) |
|---|---|
| Ver si está vivo | `systemctl status zeia-bot` |
| Ver el log en vivo | `journalctl -u zeia-bot -f` |
| Log de ayer | `journalctl -u zeia-bot --since yesterday --until today` |
| Reiniciar | `sudo systemctl restart zeia-bot` |
| Apagar | `sudo systemctl stop zeia-bot` |
| Qué avisaría ahora | `sudo -u zeiabot env HOME=/opt/zeia-bot/home /opt/zeia-bot/venv/bin/python /opt/zeia-bot/app/scripts/avisos.py --ciclo` |

Si el proceso se cae, systemd lo reinicia a los 15 s; si la EC2 se reinicia,
arranca solo.

**Cambiar una variable** (p. ej. `AVISOS_MODO=enviar`):

```bash
sudo nano /opt/zeia-bot/app/.env
sudo systemctl restart zeia-bot
```

## Actualizar el código

En la laptop, con los cambios ya en git: `venv\Scripts\python.exe deploy\bot\empaquetar.py`
(sin `--env`), `scp` del `.tar.gz` y en el servidor:

```bash
tar -xzf zeia-bot-<nuevo>.tar.gz deploy/bot/instalar.sh
sudo bash deploy/bot/instalar.sh zeia-bot-<nuevo>.tar.gz
```

Reemplaza solo el código; no toca `.env`, llaves ni `data/`. Si las pruebas o
la verificación fallan, deja corriendo la versión anterior. Si el bot estaba
encendido, lo reinicia con la nueva.

## Volver a la laptop

En el servidor `sudo systemctl disable --now zeia-bot`; en la laptop borra
`data\bot_en_servidor.txt` y corre `scripts\telegram_bot.cmd` (o reinicia
sesión).
