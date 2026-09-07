#!/usr/bin/env bash
#
# capture-destination-metrics.sh — arranca / frena los samplers de
# métricas en el servidor DESTINO (Red Hat + IBM CM V8) durante una
# prueba de stress de CMCourier.
#
# Cubre todo lo que el plan de pruebas §5.2 pide capturar del destino:
#   sar (sysstat)     → CPU, RAM, swap, paging, contexto, network, disco
#   iostat            → IOPS, throughput, latencia (await/svctm) por device
#   pidstat           → recursos por proceso ICM (icmls, icmrm)
#   ss                → conexiones TCP al puerto del CMIS, sampleadas
#   db2pd             → snapshots de la base DB2 que respalda IBM CM
#   logs CM           → tramo de TODOS los *.log del dir CM detectado
#
# Uso:
#   bash capture-destination-metrics.sh probe                  # diagnostica dónde están los logs CM
#   bash capture-destination-metrics.sh start <test-id>        # T01, T02-baseline, T03-step-8, ...
#   bash capture-destination-metrics.sh stop
#
# Pre-requisitos:
#   * sysstat instalado (sar, iostat, pidstat).
#   * iproute2 instalado (ss).
#   * Para DB2: sourcear `~/sqllib/db2profile` del usuario instance.
#   * Relojes sincronizados por NTP/chrony entre origen y destino
#     (drift < 100 ms, plan §2.3).
#
# Variables (env):
#   OUT_DIR        default /var/tmp/cmcourier-stress
#   SAMPLE_S       default 5     (intervalo en seg de sar/iostat/pidstat)
#   CMIS_PORT      default 9080
#   ICM_LOG_DIR    si lo seteás, el script usa exactamente ese dir y NO
#                  intenta auto-discovery. Sin setearlo, busca solo en
#                  9 ubicaciones típicas + find como último recurso.
#   DB2_DB         default ICMNLSDB
#
# Outputs en ${OUT_DIR}/<test-id>/:
#   sar.bin               binario sysstat — exportá con `sadf`
#   iostat.txt            extendido por device (-xz)
#   pidstat.txt           por proceso ICM
#   ss.txt                snapshots periódicos de conexiones TCP al CMIS
#   db2pd.txt             snapshots de DB2 (cada 6×SAMPLE_S)
#   logs/<name>.log       tramo del log generado durante el run, por cada
#                         *.log encontrado en el dir CM detectado
#   pids                  PIDs de los samplers (para que `stop` los frene)
#   meta.txt              start_ts, stop_ts, hostname, kernel, args
#   .icm-log-dir          dir CM resuelto (consumido por `stop`)

set -euo pipefail

OUT_DIR="${OUT_DIR:-/var/tmp/cmcourier-stress}"
SAMPLE_S="${SAMPLE_S:-5}"
CMIS_PORT="${CMIS_PORT:-9080}"
DB2_DB="${DB2_DB:-ICMNLSDB}"

# Ubicaciones típicas de IBM CM V8 — orden: más comunes primero.
_ICM_LOG_CANDIDATES=(
  "/home/icmadmin/log"
  "/home/icmadmin/Log"
  "/home/icmadmin/lsserver/log"
  "/home/icmadmin/rmserver/log"
  "/opt/IBM/db2cmv8/log"
  "/opt/IBM/db2cmv8/lsserver/log"
  "/opt/IBM/icmrm/logs"
  "/var/IBM/CM/log"
  "/var/log/icm"
)

_dir_has_icm_logs() {
  local dir="$1"
  [[ -d "$dir" ]] || return 1
  shopt -s nullglob
  local matches=("$dir"/icm*.log)
  shopt -u nullglob
  [[ ${#matches[@]} -gt 0 ]]
}

_discover_icm_log_dir() {
  # 1) Si el operador pasó ICM_LOG_DIR explícitamente y tiene logs, ganador.
  if [[ -n "${ICM_LOG_DIR:-}" ]]; then
    if _dir_has_icm_logs "${ICM_LOG_DIR}"; then
      echo "${ICM_LOG_DIR}"
      return 0
    fi
    echo "  ⚠ ICM_LOG_DIR=${ICM_LOG_DIR} no tiene logs icm*; pruebo otras ubicaciones." >&2
  fi
  # 2) Probá ubicaciones típicas.
  local c
  for c in "${_ICM_LOG_CANDIDATES[@]}"; do
    if _dir_has_icm_logs "$c"; then
      echo "$c"
      return 0
    fi
  done
  # 3) find acotado (max-depth 6, solo /opt /var /home).
  local found
  found=$(find /opt /var /home -maxdepth 6 -type f \
    \( -name 'icmls*.log' -o -name 'icmrm*.log' \) 2>/dev/null | head -1)
  if [[ -n "$found" ]]; then
    dirname "$found"
    return 0
  fi
  return 1
}

cmd="${1:-}"
case "$cmd" in
  probe)
    echo "→ Probe diagnóstico — buscando logs CM"
    echo ""
    echo "  ICM_LOG_DIR=${ICM_LOG_DIR:-<unset → auto-discovery>}"
    echo ""
    if found=$(_discover_icm_log_dir); then
      echo "✓ Encontrados logs CM en: $found"
      echo ""
      echo "Archivos detectados:"
      ls -lh "$found"/*.log 2>/dev/null | head -20
      echo ""
      echo "Para usarlo en un run real:"
      echo "  ICM_LOG_DIR=$found bash $0 start <test-id>"
    else
      echo "✗ No se encontraron logs CM (icmls*.log / icmrm*.log)."
      echo ""
      echo "Ubicaciones probadas (NO existen o no contienen logs CM):"
      for c in "${_ICM_LOG_CANDIDATES[@]}"; do
        if [[ -d "$c" ]]; then
          echo "  ◦ $c        (existe pero sin icm*.log)"
        fi
      done
      echo ""
      echo "Búsqueda completa (puede tardar — corré con sudo si hace falta):"
      echo "  sudo find / -type f \\( -name 'icmls*.log' -o -name 'icmrm*.log' \\) 2>/dev/null"
      echo ""
      echo "Cuando lo encuentres, pasalo así:"
      echo "  ICM_LOG_DIR=/tu/path bash $0 start <test-id>"
      exit 1
    fi
    ;;

  start)
    test_id="${2:-}"
    [[ -n "$test_id" ]] || { echo "uso: $0 start <test-id>" >&2; exit 2; }

    run_dir="${OUT_DIR}/${test_id}"
    mkdir -p "${run_dir}/logs"
    pids_file="${run_dir}/pids"
    : > "${pids_file}"

    {
      echo "test_id:    ${test_id}"
      echo "start_ts:   $(date -Iseconds)"
      echo "host:       $(hostname -f 2>/dev/null || hostname)"
      echo "kernel:     $(uname -srm)"
      echo "sample_s:   ${SAMPLE_S}"
      echo "cmis_port:  ${CMIS_PORT}"
      echo "db2_db:     ${DB2_DB}"
    } > "${run_dir}/meta.txt"

    echo "→ Capturando métricas del destino en ${run_dir}"

    # ----- sar: TODOS los contadores, binario, indefinido (count=0).
    sar -A "${SAMPLE_S}" -o "${run_dir}/sar.bin" 0 > /dev/null 2>&1 &
    echo "$! sar" >> "${pids_file}"

    # ----- iostat: extendido, omite zero-activity, sin tope.
    iostat -xz "${SAMPLE_S}" > "${run_dir}/iostat.txt" 2>&1 &
    echo "$! iostat" >> "${pids_file}"

    # ----- pidstat: matchea "icm" (icmls, icmrm) — CPU + RAM + I/O.
    pidstat -C "icm" -u -r -d "${SAMPLE_S}" > "${run_dir}/pidstat.txt" 2>&1 &
    echo "$! pidstat" >> "${pids_file}"

    # ----- ss: snapshot del estado de conexiones al puerto CMIS.
    (
      while true; do
        date -Iseconds
        count=$(ss -tn state established \
          "( sport = :${CMIS_PORT} or dport = :${CMIS_PORT} )" \
          2>/dev/null | tail -n +2 | wc -l)
        echo "established_to_cmis: ${count}"
        ss -tn state established \
          "( sport = :${CMIS_PORT} or dport = :${CMIS_PORT} )" 2>/dev/null \
          | head -50
        echo "---"
        sleep "${SAMPLE_S}"
      done
    ) > "${run_dir}/ss.txt" 2>&1 &
    echo "$! ss-loop" >> "${pids_file}"

    # ----- DB2: snapshots periódicos (cada 6 × SAMPLE_S — son caros).
    if command -v db2pd >/dev/null 2>&1; then
      (
        while true; do
          date -Iseconds
          db2pd -db "${DB2_DB}" \
            -agents -applications -bufferpools -tablespaces -dynamic 2>&1 || true
          echo "==="
          sleep "$(( SAMPLE_S * 6 ))"
        done
      ) > "${run_dir}/db2pd.txt" 2>&1 &
      echo "$! db2pd-loop" >> "${pids_file}"
    else
      echo "  ⚠ db2pd no disponible (sourcear db2profile o ignorar)" >&2
      echo "db2pd:      SKIPPED (not on PATH)" >> "${run_dir}/meta.txt"
    fi

    # ----- Logs CM: auto-discovery + snapshot de offsets de TODOS los
    # *.log del dir resuelto. `stop` extrae los tramos a partir de eso.
    if icm_log_dir=$(_discover_icm_log_dir); then
      echo "  ✓ Logs CM detectados en: ${icm_log_dir}"
      echo "${icm_log_dir}" > "${run_dir}/.icm-log-dir"
      echo "icm_logs:   ${icm_log_dir}" >> "${run_dir}/meta.txt"
      shopt -s nullglob
      for src in "${icm_log_dir}"/*.log; do
        name=$(basename "$src")
        wc -c < "$src" > "${run_dir}/logs/${name}.start-offset"
        echo "    · ${name} (offset $(cat "${run_dir}/logs/${name}.start-offset") bytes)"
      done
      shopt -u nullglob
    else
      echo "  ⚠ No se encontraron logs CM en ubicaciones típicas." >&2
      echo "    Corré '$0 probe' para diagnosticar, o pasá ICM_LOG_DIR=/tu/path." >&2
      echo "icm_logs:   NOT FOUND" >> "${run_dir}/meta.txt"
    fi

    echo "${run_dir}" > "${OUT_DIR}/.last-run"
    echo "✓ Samplers corriendo. Cuando termine la prueba:"
    echo "    $0 stop"
    ;;

  stop)
    last="${OUT_DIR}/.last-run"
    [[ -f "$last" ]] || { echo "ERROR: no hay run activo (¿corriste 'start'?)" >&2; exit 1; }
    run_dir="$(cat "$last")"
    pids_file="${run_dir}/pids"
    [[ -f "$pids_file" ]] || { echo "ERROR: ${pids_file} no existe" >&2; exit 1; }

    echo "→ Frenando samplers de ${run_dir}"
    while read -r pid name; do
      if kill -0 "$pid" 2>/dev/null; then
        kill -TERM "$pid" 2>/dev/null || true
        echo "  ✓ ${name} (${pid}) → SIGTERM"
      fi
    done < "$pids_file"
    sleep 1
    while read -r pid name; do
      kill -KILL "$pid" 2>/dev/null || true
    done < "$pids_file"

    # Extrae el tramo nuevo de cada *.log que tenga offset.
    if [[ -f "${run_dir}/.icm-log-dir" ]]; then
      icm_log_dir=$(cat "${run_dir}/.icm-log-dir")
      shopt -s nullglob
      for offset_file in "${run_dir}/logs/"*.start-offset; do
        name=$(basename "$offset_file" .start-offset)
        src="${icm_log_dir}/${name}"
        if [[ -f "${src}" ]]; then
          offset="$(cat "${offset_file}")"
          tail -c "+$((offset + 1))" "${src}" > "${run_dir}/logs/${name}" 2>/dev/null || true
        fi
      done
      shopt -u nullglob
    fi

    echo "stop_ts:    $(date -Iseconds)" >> "${run_dir}/meta.txt"
    rm -f "$last"

    echo "✓ Captura terminada. Datos en ${run_dir}"
    echo ""
    echo "  Convertí el sar.bin a CSV (para Excel):"
    echo "    sadf -dh ${run_dir}/sar.bin -- -A > ${run_dir}/sar.csv"
    ;;

  *)
    echo "uso: $0 probe                # diagnostica dónde están los logs CM" >&2
    echo "     $0 start <test-id>      # arranca los samplers" >&2
    echo "     $0 stop                 # los frena" >&2
    exit 2
    ;;
esac
