# Comandos de producción

El contenedor se llama `notorios-odoo-poller-orders-ms`. Usar `docker exec` para ejecutar comandos dentro del contenedor en ejecución:

```bash
# Agregar conexión (interactivo)
docker exec -it notorios-odoo-poller-orders-ms python -m src.main add

# Listar conexiones
docker exec -it notorios-odoo-poller-orders-ms python -m src.main list

# Probar conexión Odoo + webhook
docker exec -it notorios-odoo-poller-orders-ms python -m src.main test 1

# Ver sync logs
docker exec -it notorios-odoo-poller-orders-ms python -m src.main logs -c 1 -n 50

# Ver retry queue
docker exec -it notorios-odoo-poller-orders-ms python -m src.main retries -c 1

# Re-enviar webhooks
docker exec -it notorios-odoo-poller-orders-ms python -m src.main send -c 1 --last 3

# Ver logs del contenedor
docker logs notorios-odoo-poller-orders-ms --tail 100 -f

# Reiniciar servicio
docker restart notorios-odoo-poller-orders-ms
```