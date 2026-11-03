# Vista de eventos de datos en vivo, sin alertas ni persistencia

El cliente necesita ver **en tiempo real** qué salas van mal en la entrega de datos; las alertas existentes (plataforma Django, umbrales 20 min / 1 h) no dan esa visión general y con muchas salas generarían fatiga de alertas. Decidimos que esta capacidad sea una **vista en vivo dentro del monitor**: cálculo de cortes al vuelo con la cadencia real por combo, sin almacenar eventos ni enviar notificaciones. El histórico formal ya lo cubre el reporte de conectividad asíncrono; WhatsApp queda como fase 2 (solo la imagen, no alertas).

## Considered Options

- **Reusar/ajustar las alertas de la plataforma Django**: descartado — viven fuera de este repo y el cliente explícitamente no quiere notificaciones por cada sala.
- **Pipeline batch de eventos** (como el eliminado en ago-2026, con CSVs de huecos/eventos): descartado por ahora — el caso de uso es tiempo real; la historia agregada ya está en `/gaps` y el PDF semanal.
