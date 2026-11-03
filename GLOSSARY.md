# ZEIA-IA

Monitoreo y consulta conversacional de los datos de consumo energético (base `energy`) y de monitoreo ambiental de interiores (base `valhalladb`) de clientes reales.

## Language

**Combo**:
La unidad mínima del monitoreo ambiental: un indicador (CO2, humedad o temperatura) reportando desde una sala.
_Avoid_: sensor (el dispositivo físico), punto.

**Punto**:
La unidad mínima del monitoreo de energía: un punto de medición de un tablero.
_Avoid_: circuito, medidor.

**Cadencia real**:
El ritmo típico con que un combo o punto envía lecturas, medido de su propio historial reciente; suele ser 5 min en ambiental y 1 min en energía, pero puede diferir (p. ej. Zona Verde reporta cada ~10 min).
_Avoid_: frecuencia nominal, intervalo fijo.

**Corte**:
Lapso en que un combo o punto deja de enviar lecturas: uno o más slots perdidos frente a su cadencia real.
_Avoid_: hueco (término legado del análisis diario), gap, falta.

**Corte abierto**:
Un corte en curso: todavía no llegó la lectura que lo cierra.
_Avoid_: corte activo, caída.

**Minutos perdidos**:
La duración de un corte, expresada como slots perdidos × cadencia real; es la métrica con la que se ordena qué está yendo peor.
_Avoid_: tiempo caído, downtime.

**Intermitente vs caído**:
Los dos patrones de falla de un combo: muchos cortes cortos (intermitencia, típicamente enlace radio/gateway) frente a pocos cortes largos (caída, típicamente equipo o batería).
