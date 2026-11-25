"""Maquetacion del informe PDF (reportlab + graficas matplotlib).

Orden del documento:
  1. Identificacion (proyecto, periodo, muestreo, hash de datos) y resumen estadistico.
  2. Una seccion por variable: grafica, estadistica y eventos/anomalias.
  3. Valores tabulados de TODAS las variables graficadas (mismo muestreo que las graficas).
  4. Audit trail WinCC del periodo.
  5. Firmas electronicas (nombre, usuario, rol, significado, fecha/hora).
"""
from __future__ import annotations

import threading
from io import BytesIO
from pathlib import Path

import numpy as np
import pandas as pd

from .wincc_csv import epoch_ms_to_dt

_mpl_lock = threading.Lock()
_fonts_ready = False
FONT, FONT_B = "Helvetica", "Helvetica-Bold"

KIND_LABEL = {"limite_alta": "Límite alto", "limite_baja": "Límite bajo", "fallo_senal": "Fuera de rango instrumento",
              "dato_invalido": "Dato inválido (WinCC)", "hueco": "Hueco sin registros",
              "valor_congelado": "Valor congelado", "atipico": "Valor atípico"}
SEV_LABEL = {"alarma": "Alarma", "pre_alarma": "Pre-alarma", "info": "Info"}
C_BAD, C_WARN, C_ACCENT, C_MUTED = "#c62828", "#ef8f00", "#1f5fa8", "#6b7280"
VARS_PER_TABLE = 7
ROWS_PER_BLOCK = 36       # filas por bloque: un bloque (o varios lado a lado) llena una pagina


# ------------------------------------------------------------------ formato
def fdt(ms, sec=False) -> str:
    if ms is None:
        return "—"
    return epoch_ms_to_dt(ms).strftime("%d/%m/%Y %H:%M:%S" if sec else "%d/%m/%Y %H:%M")


def fnum(v, d=None) -> str:
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "—"
    if d is None:
        d = 3 if abs(v) < 10 else 2
    return f"{v:.{d}f}".replace(".", ",")


def fdur(s) -> str:
    if s is None:
        return "—"
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{round(s / 60)} min"
    if s < 86400:
        return f"{s / 3600:.1f} h".replace(".", ",")
    return f"{s / 86400:.1f} d".replace(".", ",")


def fstep(step_s: int) -> str:
    for n, u in ((86400, "día"), (3600, "h"), (60, "min")):
        if step_s % n == 0:
            k = step_s // n
            return f"{k} {u}" if u != "día" else (f"{k} días" if k > 1 else "1 día")
    return f"{step_s} s"


def var_label(v: dict) -> str:
    return v.get("alias") or v["var_name"]


def _register_fonts():
    """DejaVu Sans (incluida en matplotlib) para tener todos los caracteres (°, ³, ±, →...)."""
    global _fonts_ready, FONT, FONT_B
    if _fonts_ready:
        return
    try:
        import matplotlib
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        d = Path(matplotlib.get_data_path()) / "fonts" / "ttf"
        pdfmetrics.registerFont(TTFont("DejaVu", str(d / "DejaVuSans.ttf")))
        pdfmetrics.registerFont(TTFont("DejaVu-Bold", str(d / "DejaVuSans-Bold.ttf")))
        pdfmetrics.registerFontFamily("DejaVu", normal="DejaVu", bold="DejaVu-Bold")
        FONT, FONT_B = "DejaVu", "DejaVu-Bold"
    except Exception:  # noqa: BLE001 - se usa Helvetica
        pass
    _fonts_ready = True


# ------------------------------------------------------------------ graficas
def chart_png(res: dict, start_ms: int, end_ms: int, step_s: int) -> bytes:
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.dates import AutoDateLocator, ConciseDateFormatter
    from matplotlib.figure import Figure

    ser = res.get("series") or []
    with _mpl_lock:
        fig = Figure(figsize=(10.6, 3.3), dpi=150)
        FigureCanvasAgg(fig)
        ax = fig.add_subplot(111)
        if ser:
            a = np.array([[np.nan if x is None else x for x in p[:4]] for p in ser], dtype="float64")
            t = pd.to_datetime(a[:, 0].astype("int64"), unit="ms")
            ax.fill_between(t, a[:, 2], a[:, 3], color=C_ACCENT, alpha=0.18, linewidth=0, label="mín–máx del intervalo")
            ax.plot(t, a[:, 1], color=C_ACCENT, linewidth=1.0, label="media")
        lim = res.get("limits") or {}
        for k, c, ls in (("hh", C_BAD, "-"), ("h", C_WARN, "--"), ("l", C_WARN, "--"), ("ll", C_BAD, "-")):
            if lim.get(k) is not None:
                ax.axhline(lim[k], color=c, linestyle=ls, linewidth=0.9)
                ax.annotate(f"{k.upper()} {fnum(lim[k])}", xy=(1.0, lim[k]), xycoords=("axes fraction", "data"),
                            xytext=(3, 0), textcoords="offset points", va="center", fontsize=7, color=c)
        step_ms = step_s * 1000
        for e in [e for e in res["episodes"] if e["severity"] != "info" or e["kind"] == "hueco"][:200]:
            c = C_BAD if e["severity"] == "alarma" else C_WARN if e["severity"] == "pre_alarma" else C_MUTED
            ax.axvspan(pd.to_datetime(e["start_ms"], unit="ms"),
                       pd.to_datetime(max(e["end_ms"], e["start_ms"] + step_ms), unit="ms"),
                       color=c, alpha=0.08 if e["kind"] == "hueco" else 0.15, linewidth=0)
        ax.set_xlim(pd.to_datetime(start_ms, unit="ms"), pd.to_datetime(end_ms, unit="ms"))
        loc = AutoDateLocator()
        ax.xaxis.set_major_locator(loc)
        ax.xaxis.set_major_formatter(ConciseDateFormatter(
            loc, formats=["%Y", "%m/%Y", "%d/%m", "%H:%M", "%H:%M", "%S.%f"],
            zero_formats=["", "%Y", "%d/%m/%Y", "%d/%m", "%H:%M", "%H:%M"],
            offset_formats=["", "%Y", "%m/%Y", "%d/%m/%Y", "%d/%m/%Y", "%d/%m/%Y %H:%M"]))
        unit = res["variable"].get("unit")
        if unit:
            ax.set_ylabel(unit, fontsize=8)
        ax.tick_params(labelsize=7)
        ax.grid(True, color="#e5e7eb", linewidth=0.6)
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)
        if not ser:
            ax.text(0.5, 0.5, "Sin datos válidos en el periodo", transform=ax.transAxes, ha="center", va="center",
                    color=C_MUTED)
        else:
            ax.legend(loc="upper left", fontsize=7, frameon=False, ncol=2)
        fig.tight_layout(pad=0.4)
        buf = BytesIO()
        fig.savefig(buf, format="png")
    return buf.getvalue()


# ------------------------------------------------------------ valores tabulados
def tabulated_values(results: list[dict], t0: int, t1: int, step_s: int) -> tuple[np.ndarray, list[np.ndarray]]:
    """Rejilla comun de tiempos (inicio..fin cada step_s) y la media por intervalo de cada variable (NaN = sin dato)."""
    grid_t = np.arange(t0, t1, step_s * 1000, dtype="int64")
    cols = []
    for r in results:
        m = {p[0]: p[1] for p in (r.get("series") or []) if p[1] is not None}
        cols.append(np.array([m.get(int(x), np.nan) for x in grid_t], dtype="float64"))
    return grid_t, cols


def page_count(pdf: bytes) -> int:
    from pyhanko.pdf_utils.reader import PdfFileReader
    return int(PdfFileReader(BytesIO(pdf)).root["/Pages"]["/Count"])


# ------------------------------------------------------------------ documento
def render_pdf(payload: dict, signatures: list[dict], version: int, status: str) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import (Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table,
                                    TableStyle)
    from reportlab.pdfgen import canvas as rl_canvas

    _register_fonts()
    page = landscape(A4)
    margin = 1.3 * cm
    width = page[0] - 2 * margin

    st = {
        "h1": ParagraphStyle("h1", fontName=FONT_B, fontSize=16, leading=20, spaceAfter=4),
        "h2": ParagraphStyle("h2", fontName=FONT_B, fontSize=12, leading=15, spaceBefore=6, spaceAfter=4,
                             textColor=colors.HexColor("#111827")),
        "h3": ParagraphStyle("h3", fontName=FONT_B, fontSize=10, leading=13, spaceBefore=2, spaceAfter=2),
        "p": ParagraphStyle("p", fontName=FONT, fontSize=8.5, leading=11),
        "small": ParagraphStyle("small", fontName=FONT, fontSize=7, leading=9, textColor=colors.HexColor("#4b5563")),
        "cell": ParagraphStyle("cell", fontName=FONT, fontSize=7, leading=8.5),
        "cellb": ParagraphStyle("cellb", fontName=FONT_B, fontSize=7, leading=8.5),
    }
    P = lambda text, s="p": Paragraph(text, st[s])  # noqa: E731
    esc = lambda s: (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))  # noqa: E731

    grid = colors.HexColor("#d1d5db")
    head_bg = colors.HexColor("#eef2f7")
    red_bg, amber_bg = colors.HexColor("#fde2e2"), colors.HexColor("#fff1d6")

    def tstyle(extra=()):
        return TableStyle([("FONT", (0, 0), (-1, -1), FONT, 7), ("FONT", (0, 0), (-1, 0), FONT_B, 7),
                           ("BACKGROUND", (0, 0), (-1, 0), head_bg), ("GRID", (0, 0), (-1, -1), 0.3, grid),
                           ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 1.5),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5), *extra])

    rid, results = payload["report_id"], payload["results"]
    step_s, t0, t1 = payload["step_s"], payload["start_ms"], payload["end_ms"]
    story = []

    # 1. identificacion -----------------------------------------------------
    story.append(P(esc(payload["title"]), "h1"))
    ident = [
        ["Proyecto", esc(payload["project"]["name"]), "Informe", rid],
        ["Periodo", f"{fdt(t0)}  a  {fdt(t1)}", "Versión / estado", f"v{version} · {status.upper()}"],
        ["Muestreo", f"{fstep(step_s)} (media, mínimo y máximo por intervalo)", "Generado",
         f"{fdt(payload['generated_ms'], True)} por {esc(payload['generated_by'])}"],
        ["Variables", str(len(results)), "Hash SHA-256 datos", payload["data_hash"]],
    ]
    t = Table([[P(f"<b>{a}</b>", "cell"), P(b, "cell"), P(f"<b>{c}</b>", "cell"), P(d, "cell")] for a, b, c, d in ident],
              colWidths=[3 * cm, 9.5 * cm, 3.5 * cm, width - 16 * cm])
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.3, grid), ("BACKGROUND", (0, 0), (0, -1), head_bg),
                           ("BACKGROUND", (2, 0), (2, -1), head_bg), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    story += [t, Spacer(1, 4)]
    if payload.get("comment"):
        story += [P(f"<b>Comentario:</b> {esc(payload['comment'])}"), Spacer(1, 4)]
    story.append(P("Horas en hora local de planta (la registrada por WinCC). Estadística calculada sobre los "
                   "registros válidos de WinCC (Validity = 1) dentro del rango del instrumento.", "small"))

    story.append(P("Resumen estadístico", "h2"))
    hdr = ["Variable", "Unidad", "Mín", "Máx", "Media", "Mediana", "Prom. temporal", "Desv. est.", "% válidos",
           "LL / L / H / HH", "Alarmas", "Pre-alarmas", "Alarmas WinCC"]
    rows = [[P(h, "cellb") for h in hdr]]
    extra = []
    for i, r in enumerate(results, start=1):
        s, sm, lim = r["stats"], r["summary"], r["limits"] or {}
        lims = " / ".join(fnum(lim.get(k)) for k in ("ll", "l", "h", "hh"))
        wa = r["wincc_alarms"]
        rows.append([P(esc(var_label(r["variable"])), "cell"), esc(r["variable"].get("unit") or ""),
                     fnum(s.get("min")), fnum(s.get("max")), fnum(s.get("mean")), fnum(s.get("median")),
                     fnum(s.get("time_weighted_mean")), fnum(s.get("std")), fnum(s.get("pct_valid"), 1),
                     lims if any(lim.get(k) is not None for k in ("ll", "l", "h", "hh")) else "sin límites",
                     str(sm["alarma"]), str(sm["pre_alarma"]), str(wa["activations"]) if wa["tag"] else "—"])
        if sm["alarma"]:
            extra.append(("BACKGROUND", (10, i), (10, i), red_bg))
        if sm["pre_alarma"]:
            extra.append(("BACKGROUND", (11, i), (11, i), amber_bg))
    cw = [6.2 * cm, 1.4 * cm] + [1.55 * cm] * 6 + [1.4 * cm, 3.2 * cm, 1.3 * cm, 1.5 * cm]
    cw.append(width - sum(cw))
    t = Table(rows, colWidths=cw, repeatRows=1)
    t.setStyle(tstyle([("ALIGN", (2, 1), (-1, -1), "RIGHT"), *extra]))
    story.append(t)

    # 2. una seccion por variable ------------------------------------------
    max_eps = payload.get("max_episode_rows", 60)
    for r in results:
        v, s, sm = r["variable"], r["stats"], r["summary"]
        u = f" {v['unit']}" if v.get("unit") else ""
        story.append(PageBreak())
        story.append(P(esc(var_label(v)), "h2"))
        story.append(P(f"{esc(v['var_name'])} · grupo {esc(v['group'])} · "
                       f"{'tag ' + esc(v['tag']) if v.get('tag') else 'sin tag de alarma'} · "
                       f"registro ~{fdur(s.get('nominal_period_s'))}", "small"))
        story.append(Image(BytesIO(chart_png(r, t0, t1, step_s)), width=width, height=width * 3.3 / 10.6))
        kp = [[P(h, "cellb") for h in ("Mínimo", "Máximo", "Media", "Mediana", "Prom. temporal", "Desv. est.",
                                       "P05 / P95", "% válidos", "Cobertura", "Alarmas", "Pre-alarmas",
                                       "Alarmas WinCC")],
              [P(f"{fnum(s.get('min'))}{u}<br/>{fdt(s.get('min_t_ms'))}", "cell") if s.get("n") else "—",
               P(f"{fnum(s.get('max'))}{u}<br/>{fdt(s.get('max_t_ms'))}", "cell") if s.get("n") else "—",
               fnum(s.get("mean")) + u, fnum(s.get("median")) + u, fnum(s.get("time_weighted_mean")) + u,
               fnum(s.get("std")), f"{fnum(s.get('p05'))} / {fnum(s.get('p95'))}", f"{fnum(s.get('pct_valid'), 1)} %",
               f"{fnum(s.get('coverage_pct'), 1)} %", str(sm["alarma"]), str(sm["pre_alarma"]),
               str(r["wincc_alarms"]["activations"]) if r["wincc_alarms"]["tag"] else "—"]]
        t = Table(kp, colWidths=[width / 12] * 12)
        t.setStyle(tstyle([("ALIGN", (0, 0), (-1, -1), "CENTER")]))
        story.append(t)
        for n in r.get("notes", []):
            story.append(P("• " + esc(n), "small"))
        eps = r["episodes"]
        story.append(P(f"Eventos y anomalías ({r['episodes_total']})"
                       + (f" — se listan {min(max_eps, len(eps))}" if r["episodes_total"] > min(max_eps, len(eps)) else ""),
                       "h3"))
        if eps:
            rows = [["Severidad", "Tipo", "Inicio", "Fin", "Duración", "Pico", "Detalle"]]
            ex = []
            for i, e in enumerate(eps[:max_eps], start=1):
                det = (f"{e['level']} (límite {fnum(e.get('limit'))})" if e.get("level") else "") + \
                      (e.get("cause") or "") + (f"z = {e['z']}" if e.get("z") else "")
                rows.append([SEV_LABEL[e["severity"]], KIND_LABEL.get(e["kind"], e["kind"]), fdt(e["start_ms"], True),
                             fdt(e["end_ms"], True), fdur(e["duration_s"]),
                             fnum(e["peak"]) + u if e.get("peak") is not None else "", det])
                if e["severity"] == "alarma":
                    ex.append(("BACKGROUND", (0, i), (0, i), red_bg))
                elif e["severity"] == "pre_alarma":
                    ex.append(("BACKGROUND", (0, i), (0, i), amber_bg))
            t = Table(rows, colWidths=[2 * cm, 4.2 * cm, 3.3 * cm, 3.3 * cm, 2 * cm, 2.6 * cm, width - 17.4 * cm],
                      repeatRows=1)
            t.setStyle(tstyle([("ALIGN", (5, 1), (5, -1), "RIGHT"), *ex]))
            story.append(t)
        else:
            story.append(P("Sin eventos en el periodo.", "small"))

    # 3. valores tabulados ---------------------------------------------------
    story.append(PageBreak())
    story.append(P("Valores tabulados", "h2"))
    story.append(P(f"Valor medio de cada intervalo de {fstep(step_s)} (el mismo que se grafica). "
                   "Celda roja: fuera de HH/LL; ámbar: fuera de H/L; «—»: intervalo sin datos válidos. "
                   "Los picos dentro de un intervalo se ven en la gráfica y en la tabla de eventos.", "small"))
    story.append(Spacer(1, 3))
    grid_t, maps = tabulated_values(results, t0, t1, step_s)
    groups = [list(range(i, min(i + VARS_PER_TABLE, len(results)))) for i in range(0, len(results), VARS_PER_TABLE)]
    tcol, vcol, gap = 3.0 * cm, 3.3 * cm, 0.5 * cm
    show_sec = step_s % 60 != 0
    if not len(grid_t):
        story.append(P("Sin intervalos en el periodo.", "small"))
    for gi, g in enumerate(groups, start=1):
        if gi > 1:
            story.append(PageBreak())
        if len(groups) > 1:
            story.append(P(f"Tabla {gi} de {len(groups)}", "h3"))
        head = [P("Fecha / hora", "cellb")]
        for j in g:
            v = results[j]["variable"]
            name = var_label(v)
            name = name if len(name) <= 60 else name[:57] + "…"
            head.append(P(esc(name) + (f" [{esc(v['unit'])}]" if v.get("unit") else ""), "cellb"))
        block_w = tcol + vcol * len(g)
        # con pocas variables se ponen varios bloques de filas lado a lado (menos paginas)
        per_page = max(1, int((width + gap) // (block_w + gap)))

        def block(k0: int, k1: int):
            rows, ex = [head], []
            for ri, k in enumerate(range(k0, k1), start=1):
                row = [fdt(int(grid_t[k]), show_sec)]
                for ci, j in enumerate(g, start=1):
                    val = maps[j][k]
                    row.append(fnum(float(val)) if np.isfinite(val) else "—")
                    if np.isfinite(val):
                        lim = results[j]["limits"] or {}
                        if (lim.get("hh") is not None and val > lim["hh"]) or (lim.get("ll") is not None and val < lim["ll"]):
                            ex.append(("BACKGROUND", (ci, ri), (ci, ri), red_bg))
                        elif (lim.get("h") is not None and val > lim["h"]) or (lim.get("l") is not None and val < lim["l"]):
                            ex.append(("BACKGROUND", (ci, ri), (ci, ri), amber_bg))
                rows.append(row)
            t = Table(rows, colWidths=[tcol] + [vcol] * len(g))
            t.setStyle(tstyle([("ALIGN", (1, 1), (-1, -1), "RIGHT"), *ex]))
            return t

        page_rows = ROWS_PER_BLOCK * per_page
        for p0 in range(0, len(grid_t), page_rows):
            if p0:
                story.append(PageBreak())
            blocks = []
            for b0 in range(p0, min(p0 + page_rows, len(grid_t)), ROWS_PER_BLOCK):
                blocks.append(block(b0, min(b0 + ROWS_PER_BLOCK, len(grid_t))))
            if len(blocks) == 1:
                blocks[0].hAlign = "LEFT"
                story.append(blocks[0])
            else:
                cells, widths = [], []
                for i, b in enumerate(blocks):
                    if i:
                        cells.append("")
                        widths.append(gap)
                    cells.append(b)
                    widths.append(block_w)
                outer = Table([cells], colWidths=widths, hAlign="LEFT")
                outer.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                           ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
                story.append(outer)

    # 4. audit trail WinCC ---------------------------------------------------
    wa = payload.get("wincc_audit") or []
    story.append(PageBreak())
    story.append(P("Audit trail WinCC del periodo", "h2"))
    if wa:
        story.append(P(f"{payload.get('wincc_audit_total', len(wa))} registros en el periodo"
                       + (f"; se listan los {len(wa)} más recientes." if payload.get("wincc_audit_total", 0) > len(wa) else "."),
                       "small"))
        rows = [["Fecha / hora", "Usuario", "Objeto", "Descripción", "Comentario"]]
        for a in wa:
            rows.append([fdt(a["t_ms"], True), P(esc(a.get("user")), "cell"), P(esc(a.get("object")), "cell"),
                         P(esc(a.get("description")), "cell"), P(esc(a.get("comment")), "cell")])
        t = Table(rows, colWidths=[3.2 * cm, 3 * cm, 5.5 * cm, width - 17.7 * cm, 6 * cm], repeatRows=1)
        t.setStyle(tstyle())
        story.append(t)
    else:
        story.append(P("Sin registros de audit trail WinCC en el periodo.", "small"))

    # 5. firmas ---------------------------------------------------------------
    sig_block = [P("Firmas electrónicas", "h2"),
                 P("Cada firma se realizó con el usuario y la contraseña personales del firmante, re-ingresados en el "
                   "momento de firmar, y queda registrada en el audit trail del gestor. El significado indica la "
                   "responsabilidad asumida. Las firmas se vinculan al hash SHA-256 de los datos de este informe.",
                   "small"), Spacer(1, 4)]
    rows = [["Significado", "Nombre", "Usuario", "Rol", "Fecha y hora", "Comentario", "Registro (SHA-256)"]]
    for sgn in signatures:
        rows.append([P(f"<b>{esc(sgn['meaning'])}</b>", "cell"), P(esc(sgn.get("display_name") or sgn["username"]), "cell"),
                     esc(sgn["username"]), esc(sgn["role"]), fdt(sgn["signed_ms"], True),
                     P(esc(sgn.get("comment")), "cell"), P(sgn["record_hash"][:32] + "…", "cell")])
    for m in ("Elaborado", "Revisado", "Aprobado"):
        if not any(s["meaning"] == m for s in signatures):
            rows.append([P(f"<b>{m}</b>", "cell"), P("<i>pendiente</i>", "cell"), "", "", "", "", ""])
    t = Table(rows, colWidths=[2.4 * cm, 4.6 * cm, 3 * cm, 2.4 * cm, 3.4 * cm, width - 21.4 * cm, 5.6 * cm])
    t.setStyle(tstyle())
    sig_block += [t, Spacer(1, 6),
                  P(f"Integridad: hash SHA-256 de los datos {payload['data_hash']}. "
                    + ("El fichero PDF lleva un sello digital PAdES del gestor; cualquier modificación posterior invalida el sello. "
                       if payload.get("pades") else "")
                    + "Una copia puede verificarse en la pestaña Informes del gestor (Verificar PDF).", "small")]
    story.append(KeepTogether(sig_block))

    # pie de pagina con "pagina X de Y"
    footer = f"Informe {rid} · v{version} · {status.upper()} · datos SHA-256 {payload['data_hash'][:16]}…"

    class NumberedCanvas(rl_canvas.Canvas):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self._saved = []

        def showPage(self):
            self._saved.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            n = len(self._saved)
            for state in self._saved:
                self.__dict__.update(state)
                self.setFont(FONT, 7)
                self.setFillColor(colors.HexColor("#6b7280"))
                self.drawString(margin, 0.7 * cm, footer)
                self.drawRightString(page[0] - margin, 0.7 * cm, f"Página {self._pageNumber} de {n}")
                self.drawString(margin, page[1] - 0.8 * cm, payload["project"]["name"])
                self.drawRightString(page[0] - margin, page[1] - 0.8 * cm,
                                     f"Gestor de Registros WinCC · {payload.get('organization', '')}")
                super().showPage()
            super().save()

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=page, leftMargin=margin, rightMargin=margin, topMargin=1.3 * cm,
                            bottomMargin=1.3 * cm, title=payload["title"], author=payload["generated_by"],
                            subject=f"Informe {rid}", creator="Gestor de Registros WinCC")
    doc.build(story, canvasmaker=NumberedCanvas)
    return buf.getvalue()
