"""Informe en Excel (.xlsx) con el mismo contenido que el PDF, generado de los MISMOS datos congelados.

Hojas:
  Identificación     proyecto, periodo, muestreo, versión, hash de datos y resumen estadístico
  Estadística        indicadores completos por variable
  Valores tabulados  una fila por intervalo, una columna por variable (celdas rojo/ámbar por límites)
  Gráficas           una gráfica nativa de Excel por variable (media + límites)
  Eventos            eventos y anomalías de todas las variables
  Audit WinCC        audit trail de WinCC del periodo
  Firmas             firmas electrónicas, hash de datos, SHA-256 del PDF de la misma versión

Las hojas y la estructura del libro van protegidas (sin contraseña) para evitar cambios accidentales.
La integridad la garantizan el SHA-256 registrado en el gestor y el sello .p7s (ver pades.sign_detached).
"""
from __future__ import annotations

from datetime import datetime
from io import BytesIO

import numpy as np

from .report_pdf import KIND_LABEL, SEV_LABEL, fstep, tabulated_values, var_label
from .wincc_csv import epoch_ms_to_dt

HEAD_FILL = "EEF2F7"
RED_FILL, AMBER_FILL = "FDE2E2", "FFF1D6"
DT_FMT, DTS_FMT = "dd/mm/yyyy hh:mm", "dd/mm/yyyy hh:mm:ss"
NUM_FMT = "0.000"
SHEETS = ("Identificación", "Estadística", "Valores tabulados", "Gráficas", "Eventos", "Audit WinCC", "Firmas")
LIMIT_KEYS = ("ll", "l", "h", "hh")


def _dt(ms):
    return None if ms is None else epoch_ms_to_dt(ms)


def _num(v):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if np.isfinite(f) else None


def limit_color(val: float | None, lim: dict | None) -> str | None:
    """'red' fuera de HH/LL, 'amber' fuera de H/L, None dentro (misma regla que el PDF)."""
    if val is None or not lim:
        return None
    if (lim.get("hh") is not None and val > lim["hh"]) or (lim.get("ll") is not None and val < lim["ll"]):
        return "red"
    if (lim.get("h") is not None and val > lim["h"]) or (lim.get("l") is not None and val < lim["l"]):
        return "amber"
    return None


def render_xlsx(payload: dict, signatures: list[dict], version: int, status: str,
                pdf_sha256: str | None = None) -> bytes:
    from openpyxl import Workbook
    from openpyxl.chart import Reference, ScatterChart, Series
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.workbook.protection import WorkbookProtection

    bold = Font(bold=True)
    title_font = Font(bold=True, size=14)
    muted = Font(italic=True, color="4B5563", size=9)
    head_fill = PatternFill("solid", fgColor=HEAD_FILL)
    fills = {"red": PatternFill("solid", fgColor=RED_FILL), "amber": PatternFill("solid", fgColor=AMBER_FILL)}
    wrap = Alignment(wrap_text=True, vertical="top")

    rid, results = payload["report_id"], payload["results"]
    step_s, t0, t1 = payload["step_s"], payload["start_ms"], payload["end_ms"]
    show_sec = step_s % 60 != 0

    wb = Workbook()
    ws_id = wb.active
    ws_id.title = SHEETS[0]
    ws_st, ws_tab, ws_ch, ws_ev, ws_au, ws_sig = (wb.create_sheet(n) for n in SHEETS[1:])

    def header(ws, row, values, widths=None):
        for c, v in enumerate(values, start=1):
            cell = ws.cell(row=row, column=c, value=v)
            cell.font, cell.fill = bold, head_fill
            cell.alignment = Alignment(wrap_text=True, vertical="center")
        if widths:
            for c, w in enumerate(widths, start=1):
                ws.column_dimensions[get_column_letter(c)].width = w

    # 1. identificacion + resumen -------------------------------------------
    ws_id["A1"] = payload["title"]
    ws_id["A1"].font = title_font
    ident = [
        ("Proyecto", payload["project"]["name"]), ("Informe", rid),
        ("Periodo desde", _dt(t0)), ("Periodo hasta", _dt(t1)),
        ("Muestreo", f"{fstep(step_s)} (media, mínimo y máximo por intervalo)"),
        ("Versión / estado", f"v{version} · {status.upper()}"),
        ("Generado", _dt(payload["generated_ms"])), ("Generado por", payload["generated_by"]),
        ("Variables", len(results)), ("Hash SHA-256 de los datos", payload["data_hash"]),
        ("Organización", payload.get("organization") or ""),
    ]
    if payload.get("comment"):
        ident.append(("Comentario", payload["comment"]))
    for i, (k, v) in enumerate(ident, start=3):
        ws_id.cell(row=i, column=1, value=k).font = bold
        ws_id.cell(row=i, column=1).fill = head_fill
        c = ws_id.cell(row=i, column=2, value=v)
        if isinstance(v, datetime):
            c.number_format = DTS_FMT
        c.alignment = Alignment(horizontal="left")
    r = 3 + len(ident) + 1
    ws_id.cell(row=r, column=1, value="Horas en hora local de planta (la registrada por WinCC). Estadística sobre los "
                                     "registros válidos (Validity = 1) dentro del rango del instrumento.").font = muted
    r += 2
    ws_id.cell(row=r, column=1, value="Resumen estadístico").font = Font(bold=True, size=12)
    r += 1
    hdr = ["Variable", "Unidad", "Mín", "Máx", "Media", "Mediana", "Prom. temporal", "Desv. est.", "% válidos",
           "LL", "L", "H", "HH", "Alarmas", "Pre-alarmas", "Alarmas WinCC"]
    header(ws_id, r, hdr, [34, 26] + [11] * 14)
    for res in results:
        r += 1
        s, sm, lim = res["stats"], res["summary"], res["limits"] or {}
        wa = res["wincc_alarms"]
        row = [var_label(res["variable"]), res["variable"].get("unit") or "",
               *(_num(s.get(k)) for k in ("min", "max", "mean", "median", "time_weighted_mean", "std")),
               _num(s.get("pct_valid")), *(_num(lim.get(k)) for k in LIMIT_KEYS),
               sm["alarma"], sm["pre_alarma"], wa["activations"] if wa["tag"] else None]
        for c, v in enumerate(row, start=1):
            cell = ws_id.cell(row=r, column=c, value=v)
            if 3 <= c <= 13 and v is not None:
                cell.number_format = NUM_FMT if c != 9 else "0.0"
        if sm["alarma"]:
            ws_id.cell(row=r, column=14).fill = fills["red"]
        if sm["pre_alarma"]:
            ws_id.cell(row=r, column=15).fill = fills["amber"]

    # 2. estadistica completa ------------------------------------------------
    hdr = ["Variable", "Nombre WinCC", "Grupo", "Tag", "Unidad", "Mínimo", "Fecha mínimo", "Máximo", "Fecha máximo",
           "Media", "Mediana", "Prom. temporal", "Desv. est.", "P05", "P95", "N válidos", "Filas", "% válidos",
           "Cobertura %", "Periodo registro (s)", "Rango mín", "Rango máx", "Alarmas", "Pre-alarmas", "Info",
           "Alarmas WinCC", "Notas"]
    header(ws_st, 1, hdr, [30, 30, 18, 12, 9, 11, 17, 11, 17] + [11] * 17 + [60])
    for i, res in enumerate(results, start=2):
        v, s, sm, lim = res["variable"], res["stats"], res["summary"], res["limits"] or {}
        row = [var_label(v), v["var_name"], v.get("group"), v.get("tag"), v.get("unit"),
               _num(s.get("min")), _dt(s.get("min_t_ms")), _num(s.get("max")), _dt(s.get("max_t_ms")),
               *(_num(s.get(k)) for k in ("mean", "median", "time_weighted_mean", "std", "p05", "p95")),
               s.get("n", 0), s.get("total_rows", 0), _num(s.get("pct_valid")), _num(s.get("coverage_pct")),
               _num(s.get("nominal_period_s")), _num(lim.get("range_min")), _num(lim.get("range_max")),
               sm["alarma"], sm["pre_alarma"], sm["info"],
               res["wincc_alarms"]["activations"] if res["wincc_alarms"]["tag"] else None,
               " · ".join(res.get("notes", []))]
        for c, val in enumerate(row, start=1):
            cell = ws_st.cell(row=i, column=c, value=val)
            if isinstance(val, datetime):
                cell.number_format = DTS_FMT
            elif isinstance(val, float):
                cell.number_format = NUM_FMT
    ws_st.freeze_panes = "B2"

    # 3. valores tabulados ---------------------------------------------------
    grid_t, cols = tabulated_values(results, t0, t1, step_s)
    names = [var_label(res["variable"]) + (f" [{res['variable']['unit']}]" if res["variable"].get("unit") else "")
             for res in results]
    header(ws_tab, 1, ["Fecha / hora"] + names, [18] + [16] * len(results))
    ws_tab.row_dimensions[1].height = 45
    lims = [res["limits"] or {} for res in results]
    fmt = DTS_FMT if show_sec else DT_FMT
    for k, t in enumerate(grid_t):
        rr = k + 2
        ws_tab.cell(row=rr, column=1, value=epoch_ms_to_dt(int(t))).number_format = fmt
        for j, col in enumerate(cols):
            val = col[k]
            if not np.isfinite(val):
                continue
            cell = ws_tab.cell(row=rr, column=j + 2, value=float(val))
            cell.number_format = NUM_FMT
            color = limit_color(float(val), lims[j])
            if color:
                cell.fill = fills[color]
    n_rows = len(grid_t)
    ws_tab.freeze_panes = "B2"
    if n_rows:
        ws_tab.auto_filter.ref = f"A1:{get_column_letter(len(results) + 1)}{n_rows + 1}"
    note_col = len(results) + 3
    ws_tab.cell(row=1, column=note_col,
                value=f"Valor medio de cada intervalo de {fstep(step_s)} (el mismo que se grafica). Rojo: fuera de "
                      "HH/LL; ámbar: fuera de H/L; celda vacía: intervalo sin datos válidos.").font = muted

    # 4. graficas nativas ----------------------------------------------------
    ws_ch["A1"] = "Gráficas por variable (media por intervalo y límites de alarma)"
    ws_ch["A1"].font = Font(bold=True, size=12)
    ws_ch["A2"] = "Datos en la hoja «Valores tabulados». Tabla auxiliar de límites a la derecha (columnas Z en adelante)."
    ws_ch["A2"].font = muted
    lim_col = 26                         # Z: tabla auxiliar con los extremos de cada linea de limite
    lr = 1
    ws_ch.cell(row=lr, column=lim_col, value="Límites para las gráficas").font = bold
    anchor_row = 4
    for j, res in enumerate(results):
        if not n_rows:
            break
        ch = ScatterChart()
        ch.title = names[j][:80]
        ch.style = 13
        ch.height, ch.width = 7.5, 26
        ch.y_axis.title = res["variable"].get("unit") or ""
        ch.x_axis.number_format = "dd/mm hh:mm"
        ch.x_axis.majorTimeUnit = "days"
        ch.legend.position = "b"
        xs = Reference(ws_tab, min_col=1, min_row=2, max_row=n_rows + 1)
        ys = Reference(ws_tab, min_col=j + 2, min_row=1, max_row=n_rows + 1)
        s = Series(ys, xs, title_from_data=True)
        s.marker.symbol = "none"
        s.graphicalProperties.line.solidFill = "1F5FA8"
        s.graphicalProperties.line.width = 12700
        s.smooth = False
        ch.series.append(s)
        for key, color in (("hh", "C62828"), ("h", "EF8F00"), ("l", "EF8F00"), ("ll", "C62828")):
            val = _num(lims[j].get(key))
            if val is None:
                continue
            lr += 1
            ws_ch.cell(row=lr, column=lim_col, value=f"{names[j][:40]} {key.upper()}")
            ws_ch.cell(row=lr, column=lim_col + 1, value=epoch_ms_to_dt(int(grid_t[0]))).number_format = DT_FMT
            ws_ch.cell(row=lr, column=lim_col + 2, value=epoch_ms_to_dt(int(grid_t[-1]))).number_format = DT_FMT
            ws_ch.cell(row=lr, column=lim_col + 3, value=val)
            ws_ch.cell(row=lr, column=lim_col + 4, value=val)
            ls = Series(Reference(ws_ch, min_col=lim_col + 3, max_col=lim_col + 4, min_row=lr),
                        Reference(ws_ch, min_col=lim_col + 1, max_col=lim_col + 2, min_row=lr),
                        title=f"{key.upper()} {val:g}")
            ls.marker.symbol = "none"
            ls.graphicalProperties.line.solidFill = color
            ls.graphicalProperties.line.dashStyle = "solid" if key in ("hh", "ll") else "dash"
            ch.series.append(ls)
        ws_ch.add_chart(ch, f"A{anchor_row}")
        anchor_row += 16
    if not n_rows:
        ws_ch["A4"] = "Sin intervalos en el periodo."

    # 5. eventos ---------------------------------------------------------------
    header(ws_ev, 1, ["Variable", "Severidad", "Tipo", "Inicio", "Fin", "Duración (s)", "Pico", "Nivel", "Límite",
                      "Detalle"], [30, 11, 24, 19, 19, 12, 11, 8, 10, 40])
    r = 1
    for res in results:
        for e in res["episodes"]:
            r += 1
            det = (e.get("cause") or "") + (f"z = {e['z']}" if e.get("z") else "")
            row = [var_label(res["variable"]), SEV_LABEL.get(e["severity"], e["severity"]),
                   KIND_LABEL.get(e["kind"], e["kind"]), _dt(e["start_ms"]), _dt(e["end_ms"]), e["duration_s"],
                   _num(e.get("peak")), e.get("level"), _num(e.get("limit")), det or None]
            for c, v in enumerate(row, start=1):
                cell = ws_ev.cell(row=r, column=c, value=v)
                if isinstance(v, datetime):
                    cell.number_format = DTS_FMT
            if e["severity"] in ("alarma", "pre_alarma"):
                ws_ev.cell(row=r, column=2).fill = fills["red" if e["severity"] == "alarma" else "amber"]
        if res["episodes_total"] > len(res["episodes"]):
            r += 1
            ws_ev.cell(row=r, column=1, value=f"{var_label(res['variable'])}: {res['episodes_total']} eventos; "
                                              f"se listan los primeros {len(res['episodes'])}.").font = muted
    if r == 1:
        ws_ev.cell(row=2, column=1, value="Sin eventos en el periodo.").font = muted
    ws_ev.freeze_panes = "A2"
    if r > 1:
        ws_ev.auto_filter.ref = f"A1:J{r}"

    # 6. audit trail WinCC ---------------------------------------------------
    wa = payload.get("wincc_audit") or []
    header(ws_au, 1, ["Fecha / hora", "Usuario", "Objeto", "Descripción", "Comentario"], [19, 16, 28, 90, 30])
    for i, a in enumerate(wa, start=2):
        ws_au.cell(row=i, column=1, value=_dt(a["t_ms"])).number_format = DTS_FMT
        for c, k in enumerate(("user", "object", "description", "comment"), start=2):
            ws_au.cell(row=i, column=c, value=a.get(k)).alignment = wrap
    total = payload.get("wincc_audit_total", len(wa))
    ws_au.cell(row=len(wa) + 3, column=1,
               value=(f"{total} registros en el periodo" + (f"; se listan los {len(wa)} más recientes." if total > len(wa)
                                                            else ".")) if wa else
               "Sin registros de audit trail WinCC en el periodo.").font = muted
    ws_au.freeze_panes = "A2"

    # 7. firmas ---------------------------------------------------------------
    ws_sig["A1"] = "Firmas electrónicas"
    ws_sig["A1"].font = Font(bold=True, size=12)
    ws_sig["A2"] = ("Cada firma se realizó con el usuario y la contraseña personales del firmante, re-ingresados al "
                    "firmar, y queda registrada en el audit trail del gestor. Las firmas se vinculan al hash SHA-256 "
                    "de los datos de este informe.")
    ws_sig["A2"].font = muted
    header(ws_sig, 4, ["Significado", "Nombre", "Usuario", "Rol", "Fecha y hora", "Comentario", "Registro (SHA-256)"],
           [14, 28, 16, 13, 19, 40, 70])
    r = 4
    for sgn in signatures:
        r += 1
        row = [sgn["meaning"], sgn.get("display_name") or sgn["username"], sgn["username"], sgn["role"],
               _dt(sgn["signed_ms"]), sgn.get("comment"), sgn["record_hash"]]
        for c, v in enumerate(row, start=1):
            cell = ws_sig.cell(row=r, column=c, value=v)
            if isinstance(v, datetime):
                cell.number_format = DTS_FMT
        ws_sig.cell(row=r, column=1).font = bold
    for m in ("Elaborado", "Revisado", "Aprobado"):
        if not any(s["meaning"] == m for s in signatures):
            r += 1
            ws_sig.cell(row=r, column=1, value=m).font = bold
            ws_sig.cell(row=r, column=2, value="pendiente").font = muted
    r += 2
    integ = [("Hash SHA-256 de los datos", payload["data_hash"]), ("Informe / versión", f"{rid} · v{version}"),
             ("Estado", status.upper())]
    if pdf_sha256:
        integ.append(("SHA-256 del PDF de esta versión", pdf_sha256))
    for k, v in integ:
        ws_sig.cell(row=r, column=1, value=k).font = bold
        ws_sig.cell(row=r, column=3, value=v)
        r += 1
    r += 1
    ws_sig.cell(row=r, column=1, value=(
        "Integridad: este libro va acompañado de un sello digital (fichero .p7s, firma CMS con el certificado del "
        "gestor). Cualquier modificación del .xlsx invalida el sello. Una copia puede verificarse en la pestaña "
        "Informes del gestor (Verificar archivo) o con OpenSSL: "
        "openssl cms -verify -binary -inform DER -in <informe>.xlsx.p7s -content <informe>.xlsx "
        "-CAfile gestor_sello.pem -purpose any")).alignment = wrap
    ws_sig.merge_cells(start_row=r, start_column=1, end_row=r, end_column=7)
    ws_sig.row_dimensions[r].height = 60

    # proteccion (contra cambios accidentales) y propiedades ----------------
    for ws in wb.worksheets:
        ws.protection.sheet = True
        ws.protection.autoFilter = False      # se permite filtrar
        ws.protection.sort = False
        ws.protection.formatColumns = False   # y ajustar el ancho de columnas
        ws.sheet_view.zoomScale = 90
    wb.security = WorkbookProtection(lockStructure=True)
    wb.active = 0
    p = wb.properties
    p.title = payload["title"][:250]
    p.subject = f"Informe {rid} v{version} ({status})"
    p.creator = "Gestor de Registros WinCC"
    p.lastModifiedBy = "Gestor de Registros WinCC"
    p.keywords = rid
    p.description = f"Datos SHA-256 {payload['data_hash']}"
    p.created = p.modified = datetime.now()

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
