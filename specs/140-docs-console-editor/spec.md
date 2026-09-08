# 140 — Documentación del editor de configuración de la consola (137–139)

## Por qué

136 dejó la documentación de la consola alineada con 0.111.0, donde
"editar el YAML completo" y "dar de alta conexiones" figuraban como
cosas que la consola NO hacía. 137–139 las implementan. Documentación
que describe un límite que ya no existe es peor que ninguna: el
operador no va a buscar `[9]` si la guía le dice que no está.

## Qué

**REQ-001 — Guía `docs/how-to/probar-la-consola.md`.** Nueva sección
"Editar conexiones desde [2]" (nueva / editar / quitar / mover al
registro; qué pasa con las credenciales de sesión; qué se escribe al
YAML y qué no — los defaults vacíos no se escriben) y "Editar el
archivo completo desde [9]" (`v` valida, `w` escribe con confirmación y
backup; qué preserva — comentarios, orden, comillas, CRLF, indentación
— y qué no: los ítems de lista reemplazados pierden sus comentarios;
`connections` se edita en `[2]`; diferencia entre `[3]` overrides de
sesión y `[9]` archivo). La sección "Qué NO hace" pierde las viñetas de
edición de YAML/conexiones (si queda vacía, se elimina). Un ejercicio
guiado nuevo: crear `clientes_sql` en `[2]`, apuntar una source mssql
desde `[9]`, `v`, `w`, `4` doctor.

**REQ-002 — Referencia.** `docs/reference/cli.md`: tabla de tabs gana
`9·YAML`; tabla de teclas gana `n` (en `[2]`), `v` (en `[9]`) y `w`
aclara "en `[3]` escribe los overrides; en `[9]` el formulario". Los
archivos `.bak-*` y `.tmp` se documentan una sola vez (sección
"Archivos que escribe la consola"). `docs/reference/config-reference.
yaml`: cabecera de versión a 0.112.0 y una línea en `connections:` que
dice que se puede administrar desde `[2]`.

**REQ-003 — Tutorial y explicación.** `docs/tutorials/04-*.md` (el de la
consola): paso de alta de conexión desde `[2]` en lugar de editar el
YAML a mano, si el tutorial hoy lo hace a mano. `docs/explanation/
operations-console.md`: párrafo "Un solo escritor del YAML" — por qué
`YamlDocument` (round-trip con ruamel, verificación por recarga, backup,
atomicidad) y por qué `[3]` y `[9]` son cosas distintas. `docs/diagrams/
console-flow.md`: el diagrama de flujo de la consola gana el nodo `[9]`
y las flechas `[2]/[9] → YAML → [3]/[4]` (convención Mermaid del repo).

**REQ-004 — Ayuda en la consola.** `HelpScreen.HELP` ya lo cubre 139;
acá sólo se verifica que la ayuda, la guía y `cli.md` digan lo mismo
(mismas teclas, mismos nombres de tabs).

**REQ-005 — Verificación.** Cada tecla y cada id de tab mencionado en
la doc existe en `app.py` (`_TABS`, `BINDINGS`) — se chequea con el
script de claves del refresh 136 (engram `pattern`: "Validar Mermaid y
claves de schema en docs con scripts") adaptado a teclas.

## Escenarios

**E1 —** `rg -n "NO hace|no se puede editar|editar el YAML a mano"
docs/` no devuelve líneas que contradigan 137–139.

**E2 —** `rg -n "9·YAML|\[9\]" docs/reference/cli.md docs/how-to/
probar-la-consola.md docs/explanation/operations-console.md` da al menos
un hit por archivo.

**E3 —** Mermaid de `console-flow.md` pasa el validador del refresh 136.

## Notas de implementación

- Escribir contra el CÓDIGO (specs 137–139 pueden haber cambiado en la
  implementación); verificar teclas en `app.py`, ids en `yaml_pane.py`
  y `creds_pane.py`.
- No tocar CHANGELOG ni versiones: eso va en el commit de release.
