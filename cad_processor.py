import os
import sys
import tempfile
import subprocess
import math
import time
import ezdxf
from ezdxf import bbox, recover
from ezdxf.addons.drawing import Frontend, RenderContext, layout
from ezdxf.addons.drawing.pymupdf import PyMuPdfBackend
from ezdxf.addons.drawing.config import Configuration, ColorPolicy, BackgroundPolicy
from ezdxf.math import BoundingBox2d
from ezdxf.entities.dxfobj import SortEntsTable
import pymupdf

# Parchear SortEntsTable.load_table de ezdxf para tolerar tablas SORTENTSTABLE
# con códigos de handle corruptos generados por LibreDWG (ej. código 331 en vez de 5).
_orig_sort_ents_load_table = SortEntsTable.load_table

def _safe_sort_ents_load_table(self, tags):
    try:
        return _orig_sort_ents_load_table(self, tags)
    except Exception:
        pass

SortEntsTable.load_table = _safe_sort_ents_load_table


def _normalize_layout_viewports(doc):
    """
    Normaliza los atributos 'status' e 'id' de las entidades VIEWPORT en los Layouts.
    LibreDWG suele exportar los viewports con status=0 e id=0, lo que provoca que ezdxf
    los considere invisibles/inactivos y no renderice el contenido del Espacio Modelo.
    """
    try:
        for lay in doc.layouts:
            if lay.name == "Model":
                continue
            vps = list(lay.query("VIEWPORT"))
            for idx, vp in enumerate(vps):
                if idx == 0:
                    # El primer viewport representa la cámara general del Paper Space
                    vp.dxf.status = 1
                    if vp.dxf.get("id", 0) <= 0:
                        vp.dxf.id = 1
                else:
                    if vp.dxf.get("status", 0) <= 1:
                        vp.dxf.status = idx + 1
                    if vp.dxf.get("id", 0) <= 1:
                        vp.dxf.id = idx + 1
    except Exception:
        pass


_DXF_DOC_CACHE = {"path": None, "mtime": None, "doc": None}


def safe_read_dxf(dxf_path):
    """
    Intenta leer el archivo DXF de forma súper rápida usando ezdxf.readfile estándar.
    Si falla (por ejemplo, debido a corrupción de cabecera), recurre a recover.readfile
    para reparar estructuras de coordenadas dañadas de forma automática.
    Incluye caché en memoria para evitar releer el mismo archivo temporal repetidamente.
    """
    try:
        mtime = os.path.getmtime(dxf_path)
        if _DXF_DOC_CACHE["path"] == dxf_path and _DXF_DOC_CACHE["mtime"] == mtime and _DXF_DOC_CACHE["doc"] is not None:
            return _DXF_DOC_CACHE["doc"]
    except Exception:
        mtime = None

    try:
        # ezdxf.readfile estándar es entre 3 y 5 veces más rápido porque no realiza
        # la auditoría completa ni reparación de entidades excepto si es estrictamente necesario.
        doc = ezdxf.readfile(dxf_path)
        _normalize_layout_viewports(doc)
        _DXF_DOC_CACHE.update({"path": dxf_path, "mtime": mtime, "doc": doc})
        return doc
    except Exception as e:
        print(f"Lectura estándar falló: {e}. Intentando recuperación automática con auditoría...")
        try:
            doc, auditor = recover.readfile(dxf_path)
            _normalize_layout_viewports(doc)
            _DXF_DOC_CACHE.update({"path": dxf_path, "mtime": mtime, "doc": doc})
            return doc
        except Exception:
            raise e

def is_valid_bbox(box):
    """
    Verifica de forma estricta que una caja de límites (BoundingBox2d) sea válida,
    tenga datos válidos y no contenga coordenadas NaN o Inf.
    """
    if box is None or not box.has_data:
        return False
    try:
        # Verificar NaN e Infinitos
        for val in (box.extmin.x, box.extmin.y, box.extmax.x, box.extmax.y):
            if val is None or math.isnan(val) or math.isinf(val):
                return False
        # El área debe ser positiva y razonable
        w = box.extmax.x - box.extmin.x
        h = box.extmax.y - box.extmin.y
        if w <= 1e-3 or h <= 1e-3:
            return False
        return True
    except Exception:
        return False

def get_entity_bbox(entity, doc, cache=None):
    """
    Calcula de forma segura el bounding box de una entidad.
    Si es un bloque (INSERT) y falla el método por defecto (común con imágenes o wipeouts
    corruptos), recorre sus entidades locales de forma segura, calcula sus cajas y aplica
    la matriz de transformación del bloque.
    """
    try:
        box = bbox.extents([entity], cache=cache)
        if is_valid_bbox(box):
            return box
    except Exception:
        pass
        
    if entity.dxftype() == 'INSERT':
        try:
            from ezdxf.math import Vec3, Vec2
            block = doc.blocks[entity.dxf.name]
            boxes = []
            for sub_entity in block:
                try:
                    sub_box = bbox.extents([sub_entity], cache=cache)
                    if is_valid_bbox(sub_box):
                        boxes.append(sub_box)
                except Exception:
                    pass
            if boxes:
                m = entity.matrix44()
                x_mins = [b.extmin.x for b in boxes]
                y_mins = [b.extmin.y for b in boxes]
                x_maxs = [b.extmax.x for b in boxes]
                y_maxs = [b.extmax.y for b in boxes]
                
                local_min = Vec3(min(x_mins), min(y_mins), 0)
                local_max = Vec3(max(x_maxs), max(y_maxs), 0)
                
                # Transformar las esquinas del rectángulo local usando la matriz del bloque
                p1 = m.transform(local_min)
                p2 = m.transform(local_max)
                p3 = m.transform(Vec3(local_min.x, local_max.y, 0))
                p4 = m.transform(Vec3(local_max.x, local_min.y, 0))
                
                xs = [p.x for p in (p1, p2, p3, p4)]
                ys = [p.y for p in (p1, p2, p3, p4)]
                
                return BoundingBox2d([Vec2(min(xs), min(ys)), Vec2(max(xs), max(ys))])
        except Exception:
            pass
            
    return None

def get_resource_path(relative_path):
    """
    Obtiene la ruta absoluta de un recurso, compatible con desarrollo local
    y ejecutable empaquetado con PyInstaller.
    """
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

def get_short_path_name(long_name):
    """
    Obtiene la versión corta (8.3 format) de una ruta en Windows.
    Esto es crucial para evitar errores con caracteres no-ASCII (tildes, eñes, etc.)
    en herramientas de consola compiladas bajo entornos POSIX como LibreDWG.
    """
    if not long_name or sys.platform != "win32":
        return long_name
    try:
        import ctypes
        from ctypes import wintypes
        
        # Cargar kernel32
        _GetShortPathNameW = ctypes.windll.kernel32.GetShortPathNameW
        _GetShortPathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        _GetShortPathNameW.restype = wintypes.DWORD
        
        # Primero calcular el tamaño necesario del búfer
        buf_size = _GetShortPathNameW(long_name, None, 0)
        if buf_size == 0:
            # Si el archivo o directorio no existe aún, intentamos resolver
            # su directorio padre y concatenar el nombre del archivo
            parent, child = os.path.split(long_name)
            if parent:
                parent_short = get_short_path_name(parent)
                return os.path.join(parent_short, child)
            return long_name
            
        buf = ctypes.create_unicode_buffer(buf_size)
        _GetShortPathNameW(long_name, buf, buf_size)
        return buf.value
    except Exception as e:
        print(f"Error al obtener short path name para {long_name}: {e}")
        return long_name

def convert_dwg_to_dxf(dwg_path, dxf_path):
    """
    Convierte un archivo DWG a DXF usando el ejecutable dwg2dxf de LibreDWG.
    """
    dwg2dxf_exe = get_resource_path(os.path.join("libredwg_bin", "dwg2dxf.exe"))
    if not os.path.exists(dwg2dxf_exe):
        # Intentar en la carpeta local de desarrollo si no se encuentra en el ejecutable
        dwg2dxf_exe = os.path.join(os.path.dirname(__file__), "libredwg_bin", "dwg2dxf.exe")
        if not os.path.exists(dwg2dxf_exe):
            raise FileNotFoundError(f"No se encontró el convertidor LibreDWG en {dwg2dxf_exe}")
            
    # Convertir rutas a formato corto (8.3) para evitar problemas con espacios y caracteres especiales/no-ASCII
    dwg2dxf_exe_short = get_short_path_name(dwg2dxf_exe)
    dxf_path_short = get_short_path_name(dxf_path)
    dwg_path_short = get_short_path_name(dwg_path)
    
    cmd = [dwg2dxf_exe_short, "-y", "-o", dxf_path_short, dwg_path_short]
    print(f"Ejecutando: {' '.join(cmd)}")
    
    # Ocultar ventana de consola en Windows al ejecutar el proceso
    startupinfo = None
    if sys.platform == "win32":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        
    # Configurar variables de entorno agregando la carpeta de LibreDWG al PATH para la resolución de sus DLLs
    env = os.environ.copy()
    exe_dir = os.path.dirname(dwg2dxf_exe)
    if exe_dir:
        env["PATH"] = os.path.abspath(exe_dir) + os.pathsep + env.get("PATH", "")
        
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        startupinfo=startupinfo,
        env=env,
        check=False
    )
    
    if result.returncode != 0:
        raise RuntimeError(f"Error al convertir DWG a DXF (código {result.returncode}):\n{result.stderr}")
        
    if not os.path.exists(dxf_path):
        raise FileNotFoundError(f"La conversión finalizó, pero no se generó el archivo de salida en {dxf_path}")
        
    return dxf_path

def pre_analyze_dxf(dxf_path):
    """
    Analiza de forma rápida el archivo DXF para extraer:
    1. Lista de presentaciones (Layouts/Paper Space).
    2. Lista de nombres de bloques (INSERT) y su frecuencia en el espacio modelo.
    3. Capas (Layers) disponibles en el espacio modelo que tienen geometrías/líneas.
    """
    doc = safe_read_dxf(dxf_path)
    msp = doc.modelspace()
    
    # 1. Pestañas de presentación (Layouts) con contenido real
    # Excluimos "Model" y descartamos layouts por defecto vacíos (sin geometrías ni viewports al modelo)
    layouts = []
    for lay in doc.layouts:
        if lay.name == "Model":
            continue
        entities_list = list(lay)
        if not entities_list:
            continue
        non_vp = [e for e in entities_list if e.dxftype() != "VIEWPORT"]
        vps = [e for e in entities_list if e.dxftype() == "VIEWPORT"]
        if len(non_vp) > 0 or len(vps) > 1:
            layouts.append(lay.name)
    
    # 2. Bloques en espacio modelo
    block_counts = {}
    for entity in msp.query('INSERT'):
        name = entity.dxf.name
        block_counts[name] = block_counts.get(name, 0) + 1
        
    blocks = sorted(list(block_counts.items()), key=lambda x: -x[1])
    
    # 3. Capas de polilíneas y líneas (candidatas a contener contornos de planos)
    layers = set()
    for entity in msp.query('LWPOLYLINE POLYLINE LINE'):
        layers.add(entity.dxf.layer)
            
    return {
        "layouts": layouts,
        "blocks": blocks,
        "layers": sorted(list(layers))
    }

def sort_boxes(boxes):
    """
    Ordena una lista de BoundingBox2d simulando orden de lectura (de arriba a abajo, de izquierda a derecha).
    Usa una tolerancia vertical basada en la altura promedio de las cajas.
    """
    if not boxes:
        return []
        
    # Calcular altura promedio para la tolerancia de alineación vertical
    avg_height = sum((b.extmax.y - b.extmin.y) for b in boxes) / len(boxes)
    tolerance = avg_height * 0.5  # 50% de la altura promedio como tolerancia
    
    # Ordenar por Y descendente primero (arriba a abajo)
    sorted_boxes = sorted(boxes, key=lambda b: -b.extmax.y)
    
    grouped_boxes = []
    current_group = []
    
    for box in sorted_boxes:
        if not current_group:
            current_group.append(box)
        else:
            # Si el borde superior está dentro de la tolerancia, pertenecen a la misma "fila"
            ref_box = current_group[0]
            if abs(ref_box.extmax.y - box.extmax.y) < tolerance:
                current_group.append(box)
            else:
                # Ordenar la fila actual de izquierda a derecha (X ascendente)
                grouped_boxes.extend(sorted(current_group, key=lambda b: b.extmin.x))
                current_group = [box]
                
    if current_group:
        grouped_boxes.extend(sorted(current_group, key=lambda b: b.extmin.x))
        
    return grouped_boxes

def get_polyline_points(entity):
    """Obtiene los puntos 2D de una polilínea (LWPOLYLINE o POLYLINE)."""
    if entity.dxftype() == 'LWPOLYLINE':
        return [(float(p[0]), float(p[1])) for p in entity.get_points()]
    elif entity.dxftype() == 'POLYLINE':
        return [(float(p.dxf.location.x), float(p.dxf.location.y)) for p in entity.vertices]
    return []

def is_geometrically_closed(entity):
    """
    Verifica si una polilínea es cerrada geométricamente,
    incluso si su bandera is_closed es False en AutoCAD.
    Usa una tolerancia dinámica basada en las unidades del dibujo (5 mm).
    """
    try:
        if entity.is_closed:
            return True
    except Exception:
        pass
    if entity.dxftype() in ('LWPOLYLINE', 'POLYLINE'):
        try:
            points = get_polyline_points(entity)
        except Exception:
            return False
        if len(points) >= 3:
            p1 = points[0]
            p2 = points[-1]
            
            # Obtener tolerancia basada en las unidades del dibujo
            try:
                doc = entity.doc
                insunits = doc.units
            except Exception:
                insunits = 4  # mm por defecto
                
            units_to_mm = {
                1: 25.4,     # Inches
                2: 304.8,    # Feet
                3: 1609344.0,# Miles
                4: 1.0,      # Millimeters
                5: 10.0,     # Centimeters
                6: 1000.0,   # Meters
                7: 1000000.0,# Kilometers
                10: 914.4,   # Yards
            }
            scale_to_mm = units_to_mm.get(insunits, 1.0)
            
            # Tolerancia de 20 mm convertida a unidades del dibujo
            tolerance = 20.0 / scale_to_mm
            return math.hypot(p1[0] - p2[0], p1[1] - p2[1]) < tolerance
    return False

def _intersection_area(b1, b2):
    x_min = max(b1.extmin.x, b2.extmin.x)
    y_min = max(b1.extmin.y, b2.extmin.y)
    x_max = min(b1.extmax.x, b2.extmax.x)
    y_max = min(b1.extmax.y, b2.extmax.y)
    if x_min < x_max and y_min < y_max:
        return (x_max - x_min) * (y_max - y_min)
    return 0.0

def filter_nested_boxes(boxes):
    """
    Filtra rectángulos concéntricos o anidados (ej. borde exterior del papel y margen interno).
    Conserva únicamente el rectángulo exterior (más grande), el cual representa el formato del plano.
    """
    if not boxes:
        return []
    # Ordenar por área descendente para procesar los más grandes primero
    sorted_boxes = sorted(
        boxes, 
        key=lambda b: (b.extmax.x - b.extmin.x) * (b.extmax.y - b.extmin.y), 
        reverse=True
    )
    unique_boxes = []
    
    for box in sorted_boxes:
        is_nested = False
        area_box = (box.extmax.x - box.extmin.x) * (box.extmax.y - box.extmin.y)
        if area_box <= 0:
            continue
            
        for kept in unique_boxes:
            inter_area = _intersection_area(kept, box)
            # Si más del 70% del área de box está contenida dentro de kept, se considera anidado/interno
            if inter_area > 0.7 * area_box:
                is_nested = True
                break
        if not is_nested:
            unique_boxes.append(box)
            
    return unique_boxes

def _merge_collinear_1d(lines, coord_key, min_key, max_key, coord_tol, gap_tol):
    """
    Une segmentos colineales que se tocan o solapan para reconstruir bordes
    de planos divididos en varios segmentos LINE. Conserva tanto los segmentos
    originales como los fusionados para soportar grillas de planos contiguos.
    """
    if not lines:
        return []
    sorted_lines = sorted(lines, key=lambda l: (l[coord_key], l[min_key]))
    merged_extra = []
    n = len(sorted_lines)
    i = 0
    while i < n:
        group = [sorted_lines[i]]
        j = i + 1
        while j < n and abs(sorted_lines[j][coord_key] - sorted_lines[i][coord_key]) <= coord_tol:
            group.append(sorted_lines[j])
            j += 1
        if len(group) > 1:
            group.sort(key=lambda l: l[min_key])
            cur_min = group[0][min_key]
            cur_max = group[0][max_key]
            cur_coord_sum = group[0][coord_key]
            cur_count = 1
            layer = group[0]["layer"]
            for seg in group[1:]:
                if seg[min_key] <= cur_max + gap_tol:
                    cur_max = max(cur_max, seg[max_key])
                    cur_coord_sum += seg[coord_key]
                    cur_count += 1
                else:
                    if cur_count > 1:
                        merged_extra.append({
                            coord_key: cur_coord_sum / cur_count,
                            min_key: cur_min,
                            max_key: cur_max,
                            "length": cur_max - cur_min,
                            "layer": layer
                        })
                    cur_min = seg[min_key]
                    cur_max = seg[max_key]
                    cur_coord_sum = seg[coord_key]
                    cur_count = 1
                    layer = seg["layer"]
            if cur_count > 1:
                merged_extra.append({
                    coord_key: cur_coord_sum / cur_count,
                    min_key: cur_min,
                    max_key: cur_max,
                    "length": cur_max - cur_min,
                    "layer": layer
                })
        i = j
    return lines + merged_extra

def _fast_msp_extents(msp, cache=None):
    """
    Calcula la extensión general del Espacio Modelo de manera ultrarrápida a partir
    de los vértices de líneas y polilíneas, evitando evaluar miles de textos y sombreados.
    """
    from ezdxf.math import BoundingBox2d, Vec2
    xs = []
    ys = []
    for line in msp.query('LINE'):
        try:
            s = line.dxf.start
            e = line.dxf.end
            if not (math.isnan(s.x) or math.isnan(s.y) or math.isinf(s.x) or math.isinf(s.y)):
                xs.append(s.x)
                ys.append(s.y)
            if not (math.isnan(e.x) or math.isnan(e.y) or math.isinf(e.x) or math.isinf(e.y)):
                xs.append(e.x)
                ys.append(e.y)
        except Exception:
            pass
    for poly in msp.query('LWPOLYLINE POLYLINE'):
        try:
            for px, py in get_polyline_points(poly):
                if not (math.isnan(px) or math.isnan(py) or math.isinf(px) or math.isinf(py)):
                    xs.append(px)
                    ys.append(py)
        except Exception:
            pass
    if xs and ys:
        box = BoundingBox2d([Vec2(min(xs), min(ys)), Vec2(max(xs), max(ys))])
        if is_valid_bbox(box):
            return box
    try:
        return bbox.extents(msp, cache=cache)
    except Exception:
        return None


def find_rectangles_from_lines(msp, min_size=(0.05, 0.05), cache=None):
    """
    Intenta reconstruir rectángulos cerrados y alineados a los ejes a partir
    de entidades LINE individuales de manera ultra rápida (O(H^2 log V)).
    Usa una longitud de línea mínima calculada dinámicamente según la escala del dibujo.
    """
    from ezdxf.math import BoundingBox2d, Vec2, Vec3
    import math
    import bisect
    
    # Extraer todos los segmentos de línea de LINE, LWPOLYLINE y POLYLINE
    all_segments = []
    
    # 1. Entidades LINE
    for line in msp.query('LINE'):
        try:
            all_segments.append((line.dxf.start, line.dxf.end, line.dxf.layer))
        except Exception:
            pass
            
    # 2. Entidades LWPOLYLINE y POLYLINE
    for poly in msp.query('LWPOLYLINE POLYLINE'):
        try:
            points = get_polyline_points(poly)
            if len(points) >= 2:
                is_closed = poly.is_closed or is_geometrically_closed(poly)
                for i in range(len(points) - 1):
                    p1 = Vec3(points[i][0], points[i][1], 0)
                    p2 = Vec3(points[i+1][0], points[i+1][1], 0)
                    all_segments.append((p1, p2, poly.dxf.layer))
                if is_closed:
                    p1 = Vec3(points[-1][0], points[-1][1], 0)
                    p2 = Vec3(points[0][0], points[0][1], 0)
                    all_segments.append((p1, p2, poly.dxf.layer))
        except Exception:
            pass

    if len(all_segments) < 4:
        return []
        
    doc = msp.doc
    try:
        insunits = doc.units
    except Exception:
        insunits = 4  # mm por defecto
        
    # Calcular extensión general del Espacio Modelo
    try:
        msp_box = _fast_msp_extents(msp, cache=cache)
        if msp_box and msp_box.has_data:
            w_msp = msp_box.extmax.x - msp_box.extmin.x
            h_msp = msp_box.extmax.y - msp_box.extmin.y
            max_dim = max(w_msp, h_msp)
        else:
            max_dim = 1000.0
    except Exception:
        max_dim = 1000.0

    # Heurística: Si las dimensiones generales son pequeñas (< 300), el dibujo está en metros (6),
    # incluso si la plantilla de AutoCAD dejó INSUNITS en 4 (mm) o 0 (sin especificar).
    if max_dim < 300.0 and insunits in (0, 4):
        insunits = 6
    elif insunits == 0:
        insunits = 4
            
    # Obtener el factor de conversión: 1 unidad del dibujo = X milímetros
    units_to_mm = {
        1: 25.4,     # Inches
        2: 304.8,    # Feet
        3: 1609344.0,# Miles
        4: 1.0,      # Millimeters
        5: 10.0,     # Centimeters
        6: 1000.0,   # Meters
        7: 1000000.0,# Kilometers
        10: 914.4,   # Yards
    }
    scale_to_mm = units_to_mm.get(insunits, 1.0)
    
    # El borde de una hoja debe medir al menos 100 mm. Convertimos a unidades locales.
    min_line_length = 100.0 / scale_to_mm
    
    # Pero no debe ser mayor que el 15% del tamaño total para dibujos muy pequeños, ni menor que 0.05 unidades locales.
    min_line_length = min(min_line_length, max_dim * 0.15)
    min_line_length = max(0.05, min_line_length)
    
    # Mínimo absoluto de tolerancia física (10 mm) en unidades del dibujo
    min_tol = 10.0 / scale_to_mm
        
    v_lines = []
    h_lines = []
    
    for start_pt, end_pt, layer in all_segments:
        p1 = start_pt
        p2 = end_pt
        
        dx = abs(p1.x - p2.x)
        dy = abs(p1.y - p2.y)
        length = math.hypot(dx, dy)
        
        # Omitir segmentos que son más cortos que el límite dinámico
        if length < min_line_length:
            continue
            
        # Tolerancia angular adaptativa a la escala (máx 1% de inclinación o 0.5 unidades en mm)
        tol = min(0.5 / scale_to_mm, length * 0.02)
        tol = max(tol, 1e-4)
            
        if dx <= tol and dx < dy:  # Vertical
            y_min = min(p1.y, p2.y)
            y_max = max(p1.y, p2.y)
            v_lines.append({
                "x": (p1.x + p2.x) / 2.0,
                "y_min": y_min,
                "y_max": y_max,
                "length": y_max - y_min,
                "layer": layer
            })
        elif dy <= tol and dy < dx:  # Horizontal
            x_min = min(p1.x, p2.x)
            x_max = max(p1.x, p2.x)
            h_lines.append({
                "y": (p1.y + p2.y) / 2.0,
                "x_min": x_min,
                "x_max": x_max,
                "length": x_max - x_min,
                "layer": layer
            })
            
    # Unir segmentos colineales partidos (manteniendo también los individuales)
    v_lines = _merge_collinear_1d(v_lines, "x", "y_min", "y_max", min_tol * 0.2, min_tol * 0.5)
    h_lines = _merge_collinear_1d(h_lines, "y", "x_min", "x_max", min_tol * 0.2, min_tol * 0.5)
            
    # Indexar líneas verticales por X para búsquedas binarias ultrarrápidas
    v_sorted = sorted(v_lines, key=lambda l: l["x"])
    v_xs = [v["x"] for v in v_sorted]
    
    def has_vertical_connection(x_target, y_min_target, y_max_target, length_target, align_tol, length_tol):
        # Búsqueda binaria del rango de coordenadas X candidatas
        idx = bisect.bisect_left(v_xs, x_target - align_tol)
        while idx < len(v_sorted) and v_sorted[idx]["x"] <= x_target + align_tol:
            v = v_sorted[idx]
            if abs(v["y_min"] - y_min_target) < align_tol and \
               abs(v["y_max"] - y_max_target) < align_tol and \
               abs(v["length"] - length_target) < length_tol:
                return True
            idx += 1
        return False

    rect_candidates = []
    
    # Emparejar líneas horizontales (tienen que compartir X similar y estar en Y distintos)
    h_count = len(h_lines)
    for i in range(h_count):
        h1 = h_lines[i]
        for j in range(i + 1, h_count):
            h2 = h_lines[j]
            
            # Asegurar que h1 sea la de abajo y h2 la de arriba
            if h1["y"] > h2["y"]:
                h_bottom, h_top = h2, h1
            else:
                h_bottom, h_top = h1, h2
                
            height = h_top["y"] - h_bottom["y"]
            if height < min_size[1]:
                continue
                
            # Deben tener anchos similares y coincidir en X
            width_avg = (h_bottom["length"] + h_top["length"]) / 2.0
            if width_avg < min_size[0]:
                continue
                
            # Usar tolerancia del 5% del ancho, pero no menor que el mínimo absoluto
            length_tol = max(min_tol, width_avg * 0.05)
            align_tol = max(min_tol, width_avg * 0.05)
            
            if abs(h_bottom["length"] - h_top["length"]) > length_tol:
                continue
            if abs(h_bottom["x_min"] - h_top["x_min"]) > align_tol or \
               abs(h_bottom["x_max"] - h_top["x_max"]) > align_tol:
                continue
                
            x_min = (h_bottom["x_min"] + h_top["x_min"]) / 2.0
            x_max = (h_bottom["x_max"] + h_top["x_max"]) / 2.0
            y_min = h_bottom["y"]
            y_max = h_top["y"]
            
            # Buscar si existen las dos líneas verticales que cierran el rectángulo
            h_len_tol = max(min_tol, height * 0.05)
            h_align_tol = max(min_tol, height * 0.05)
            
            if has_vertical_connection(x_min, y_min, y_max, height, h_align_tol, h_len_tol) and \
               has_vertical_connection(x_max, y_min, y_max, height, h_align_tol, h_len_tol):
                box = BoundingBox2d([Vec2(x_min, y_min), Vec2(x_max, y_max)])
                rect_candidates.append(box)
                
    return rect_candidates


def extract_modelspace_boxes_from_viewports(doc):
    """
    Extrae las regiones del Espacio Modelo hacia las que apuntan los VIEWPORTs
    de las pestañas de presentación (Layouts). Es útil como respaldo cuando el dibujo
    en el Espacio Modelo no tiene marcos de lámina dibujados directamente allí,
    sino que los cortes de hoja están definidos por los Viewports de los Layouts.
    """
    from ezdxf.math import BoundingBox2d, Vec2, Vec3
    vp_entries = []
    for lay in doc.layouts:
        if lay.name == "Model":
            continue
        vps = list(lay.query("VIEWPORT"))
        for idx, vp in enumerate(vps):
            if idx == 0:
                continue  # Omitir cámara global del Paper Space
            try:
                vh = float(vp.dxf.get("view_height", 0.0))
                pw = float(vp.dxf.get("width", 0.0))
                ph = float(vp.dxf.get("height", 0.0))
                if vh > 1e-2 and pw > 1e-2 and ph > 1e-2:
                    vw = vh * (pw / ph)
                    vt = vp.dxf.get("view_target_point", Vec3(0, 0, 0))
                    vc = vp.dxf.get("view_center_point", Vec3(0, 0, 0))
                    cx = float(vt.x + vc.x)
                    cy = float(vt.y + vc.y)
                    # Ignorar viewports por defecto que apuntan al origen vacío cuando el modelo está lejos
                    box = BoundingBox2d([Vec2(cx - vw / 2.0, cy - vh / 2.0), Vec2(cx + vw / 2.0, cy + vh / 2.0)])
                    if is_valid_bbox(box):
                        vp_entries.append((box, pw * ph))
            except Exception:
                pass
    if not vp_entries:
        return []
    # Conservar únicamente los viewports principales de lámina (>= 35% del área en papel del mayor)
    max_paper_area = max(pa for _, pa in vp_entries)
    main_boxes = [b for b, pa in vp_entries if pa >= 0.35 * max_paper_area]
    return filter_nested_boxes(main_boxes)


def detect_sheets_in_modelspace(dxf_path, method="auto", block_name=None, layer_name=None, min_size=(0.05, 0.05), sort_sheets=True, allow_viewport_fallback=True):
    """
    Detecta áreas de planos en el Espacio Modelo del DXF.
    Retorna una lista de objetos BoundingBox2d ordenados y filtrados.
    """
    doc = safe_read_dxf(dxf_path)
    msp = doc.modelspace()
    cache = bbox.Cache()
    
    # Determinar unidades del documento para ajustar el tamaño mínimo por defecto
    try:
        insunits = doc.units
    except Exception:
        insunits = 4  # mm por defecto
        
    try:
        msp_box = _fast_msp_extents(msp, cache=cache)
        if msp_box and msp_box.has_data:
            max_dim = max(msp_box.extmax.x - msp_box.extmin.x, msp_box.extmax.y - msp_box.extmin.y)
        else:
            max_dim = 1000.0
    except Exception:
        msp_box = None
        max_dim = 1000.0
        
    if max_dim < 300.0 and insunits in (0, 4):
        insunits = 6
    elif insunits == 0:
        insunits = 4
        
    # Obtener el factor de conversión: 1 unidad del dibujo = X milímetros
    units_to_mm = {
        1: 25.4,     # Inches
        2: 304.8,    # Feet
        3: 1609344.0,# Miles
        4: 1.0,      # Millimeters
        5: 10.0,     # Centimeters
        6: 1000.0,   # Meters
        7: 1000000.0,# Kilometers
        10: 914.4,   # Yards
    }
    scale_to_mm = units_to_mm.get(insunits, 1.0)
            
    # El tamaño mínimo de una hoja debe ser de al menos 150mm x 150mm en unidades del dibujo
    min_size_units = (150.0 / scale_to_mm, 150.0 / scale_to_mm)
    
    # Si min_size es el valor por defecto extremadamente pequeño, lo actualizamos al dinámico
    if min_size == (0.05, 0.05):
        min_size = min_size_units
        
    candidates = []
    
    if method == "block" and block_name:
        # Buscar por inserciones de un bloque específico
        for entity in msp.query(f'INSERT[name=="{block_name}"]'):
            try:
                entity_box = get_entity_bbox(entity, doc, cache=cache)
                if not is_valid_bbox(entity_box):
                    continue
                # Validar que tenga dimensiones mínimas válidas
                w = entity_box.extmax.x - entity_box.extmin.x
                h = entity_box.extmax.y - entity_box.extmin.y
                if w >= min_size[0] and h >= min_size[1]:
                    candidates.append(entity_box)
            except Exception as e:
                print(f"Error calculando bounding box para el bloque {block_name}: {e}")
                
    elif method == "polyline":
        # Buscar polilíneas cerradas (LWPOLYLINE y POLYLINE)
        if layer_name:
            entities = list(msp.query(f'LWPOLYLINE[layer=="{layer_name}"]')) + list(msp.query(f'POLYLINE[layer=="{layer_name}"]'))
        else:
            entities = list(msp.query('LWPOLYLINE')) + list(msp.query('POLYLINE'))
            
        for entity in entities:
            if is_geometrically_closed(entity):
                try:
                    entity_box = get_entity_bbox(entity, doc, cache=cache)
                    if not is_valid_bbox(entity_box):
                        continue
                    w = entity_box.extmax.x - entity_box.extmin.x
                    h = entity_box.extmax.y - entity_box.extmin.y
                    if w >= min_size[0] and h >= min_size[1]:
                        candidates.append(entity_box)
                except Exception as e:
                    print(f"Error calculando bounding box para polilínea: {e}")
                    
    else:  # method == "auto" (Detección automática de rectángulos estándar A0-A4 y personalizados)
        # 1. Buscar polilíneas cerradas
        entities = list(msp.query('LWPOLYLINE')) + list(msp.query('POLYLINE'))
        for entity in entities:
            if is_geometrically_closed(entity):
                try:
                    entity_box = get_entity_bbox(entity, doc, cache=cache)
                    if not is_valid_bbox(entity_box):
                        continue
                    w = entity_box.extmax.x - entity_box.extmin.x
                    h = entity_box.extmax.y - entity_box.extmin.y
                    
                    if w >= min_size[0] and h >= min_size[1]:
                        # Aceptar un rango de aspecto amplio para formatos apaisados, verticales y alargados (1.0 a 6.0)
                        ratio = max(w, h) / min(w, h)
                        if 1.0 <= ratio <= 6.0:
                            candidates.append(entity_box)
                except Exception:
                    pass
                    
        # 2. Buscar rectángulos compuestos por líneas sueltas
        try:
            line_rects = find_rectangles_from_lines(msp, min_size=min_size, cache=cache)
            candidates.extend(line_rects)
        except Exception as e:
            print(f"Error al reconstruir rectángulos desde líneas sueltas: {e}")
            
        # 3. Buscar inserciones de bloques (INSERT) que puedan ser marcos de planos
        for entity in msp.query('INSERT'):
            try:
                entity_box = get_entity_bbox(entity, doc, cache=cache)
                if not is_valid_bbox(entity_box):
                    continue
                w = entity_box.extmax.x - entity_box.extmin.x
                h = entity_box.extmax.y - entity_box.extmin.y
                if w >= min_size[0] and h >= min_size[1]:
                    ratio = max(w, h) / min(w, h)
                    # Relación de aspecto entre 1.0 y 6.0 para láminas
                    if 1.0 <= ratio <= 6.0:
                        candidates.append(entity_box)
            except Exception:
                pass
                    
    # Filtrar por área relativa para conservar formatos de láminas (incluyendo mezclas p.ej. A1/A2 o láminas de distinto ancho)
    # y descartar detalles pequeños, cajetines sueltos, notas o tablas de menor tamaño
    if candidates:
        areas = [(b.extmax.x - b.extmin.x) * (b.extmax.y - b.extmin.y) for b in candidates]
        max_area = max(areas)
        candidates = [box for box, area in zip(candidates, areas) if area >= 0.5 * max_area]

    # Filtrar rectángulos anidados (ej. conservar solo el marco exterior)
    if candidates:
        candidates = filter_nested_boxes(candidates)
        
    # Respaldo: Si en modo automático no hay marcos explícitos en el Espacio Modelo,
    # verificar si los Layouts definen cortes sobre el Espacio Modelo mediante VIEWPORTs
    if not candidates and method == "auto" and allow_viewport_fallback:
        vp_boxes = extract_modelspace_boxes_from_viewports(doc)
        if msp_box and is_valid_bbox(msp_box):
            # Conservar únicamente los viewports que intersectan realmente con la geometría del Espacio Modelo
            vp_boxes = [b for b in vp_boxes if _intersection_area(b, msp_box) > 0]
        if vp_boxes:
            candidates = vp_boxes
        elif msp_box and is_valid_bbox(msp_box):
            # Último respaldo: encuadre completo del Espacio Modelo
            candidates = [msp_box]
        
    if sort_sheets:
        candidates = sort_boxes(candidates)
        
    return candidates

def process_file_to_pdf(dwg_path, output_pdf_path, config, progress_callback=None):
    """
    Función principal que orquesta la conversión de DWG a PDF.
    
    config es un diccionario con las siguientes claves:
    - mode: "layouts" o "modelspace"
    - ms_method: "auto", "block" o "polyline"
    - block_name: (str) nombre del bloque si se selecciona ese método
    - layer_name: (str) nombre de la capa si se selecciona ese método
    - combine_pdf: (bool) si se combinan en un solo PDF o se guardan por separado
    - orientation: "auto", "landscape" o "portrait"
    """
    existing_dxf = config.get("temp_dxf_path")
    reuse_dxf = bool(existing_dxf and os.path.exists(existing_dxf))
    if reuse_dxf:
        temp_dxf_path = existing_dxf
    else:
        temp_dir = tempfile.gettempdir()
        dxf_name = f"temp_blueprint_process_{os.getpid()}_{int(time.time())}.dxf"
        temp_dxf_path = os.path.join(temp_dir, dxf_name)
    
    try:
        if not reuse_dxf:
            if progress_callback:
                progress_callback("Convirtiendo DWG a DXF...", 0.1)
            convert_dwg_to_dxf(dwg_path, temp_dxf_path)
        else:
            if progress_callback:
                progress_callback("Preparando documento DXF...", 0.1)
        
        doc = safe_read_dxf(temp_dxf_path)
        ctx = RenderContext(doc)
        
        pdf_pages_bytes = []
        
        if config.get("mode") == "layouts":
            # Obtener layouts activos (Paper Space)
            layout_names = config.get("selected_layouts")
            if not layout_names:
                layout_names = [lay.name for lay in doc.layouts if lay.name != "Model"]
            
            if not layout_names:
                raise ValueError("El archivo no contiene pestañas de presentación (Layouts/Paper Space).")
                
            total_layouts = len(layout_names)
            valid_layouts_processed = 0
            
            for idx, lay_name in enumerate(layout_names):
                lay_obj = doc.layout(lay_name)
                
                # Verificar si el layout contiene elementos con límites válidos (evita layouts vacíos por defecto)
                lay_box = bbox.extents(lay_obj)
                if not is_valid_bbox(lay_box):
                    print(f"Layout '{lay_name}' está vacío o no contiene elementos válidos. Omitiendo.")
                    continue
                
                if progress_callback:
                    progress_callback(f"Renderizando Layout '{lay_name}' ({idx+1}/{total_layouts})...", 0.2 + (idx/total_layouts)*0.6)
                
                backend = PyMuPdfBackend()
                color_mode = config.get("color_mode", "color")
                draw_config = Configuration(
                    background_policy=BackgroundPolicy.WHITE,
                    color_policy=ColorPolicy.BLACK if color_mode == "monochrome" else ColorPolicy.COLOR
                )
                Frontend(ctx, backend, config=draw_config).draw_layout(lay_obj)
                
                # Configurar página. Si se puede, leer dimensiones del layout
                try:
                    page = layout.Page.from_dxf_layout(lay_obj)
                    # Forzar dimensiones válidas si se leen como 0 o NaN
                    if page.width <= 1e-2 or page.height <= 1e-2 or math.isnan(page.width) or math.isnan(page.height):
                        page = layout.Page(297, 210, layout.Units.mm)
                except Exception:
                    # Alternativa por defecto A4 horizontal
                    page = layout.Page(297, 210, layout.Units.mm)
                    
                try:
                    pdf_bytes = backend.get_pdf_bytes(page)
                    pdf_pages_bytes.append((f"Layout_{lay_name}", pdf_bytes))
                    valid_layouts_processed += 1
                except ValueError as ve:
                    if "empty" in str(ve).lower() or "bounding box" in str(ve).lower():
                        print(f"Layout '{lay_name}' no contiene elementos graficables. Omitiendo.")
                        continue
                    else:
                        raise ve
                
            if valid_layouts_processed == 0:
                raise ValueError("Todas las pestañas de presentación (Layouts) en el archivo están vacías o no contienen geometrías válidas.")
                
        else:
            # Procesar espacio modelo usando encuadres seleccionados
            sheets = config.get("selected_sheets")
            if sheets is None:
                if progress_callback:
                    progress_callback("Analizando espacio modelo y detectando hojas/rótulos...", 0.3)
                    
                sheets = detect_sheets_in_modelspace(
                    temp_dxf_path,
                    method=config.get("ms_method", "auto"),
                    block_name=config.get("block_name"),
                    layer_name=config.get("layer_name")
                )
            
            if not sheets:
                raise ValueError("No se detectó ninguna hoja/rótulo en el espacio modelo con los criterios seleccionados.")
                
            total_sheets = len(sheets)
            msp = doc.modelspace()
            
            color_mode = config.get("color_mode", "color")
            draw_config = Configuration(
                background_policy=BackgroundPolicy.WHITE,
                color_policy=ColorPolicy.BLACK if color_mode == "monochrome" else ColorPolicy.COLOR
            )
            master_backend = PyMuPdfBackend()
            Frontend(ctx, master_backend, config=draw_config).draw_layout(msp)
            render_settings = layout.Settings(crop_at_margins=True)
            
            for idx, sheet_box in enumerate(sheets):
                if progress_callback:
                    progress_callback(f"Renderizando plano {idx+1}/{total_sheets}...", 0.4 + (idx/total_sheets)*0.5)
                
                w = sheet_box.extmax.x - sheet_box.extmin.x
                h = sheet_box.extmax.y - sheet_box.extmin.y
                
                # Determinar dimensiones de página basadas en el aspecto real en mm
                is_landscape = w > h
                
                # Ajustar orientación según configuración
                config_orient = config.get("orientation", "auto")
                if config_orient == "landscape":
                    page_w, page_h = 297, 210
                elif config_orient == "portrait":
                    page_w, page_h = 210, 297
                else:  # auto
                    page_w, page_h = (297, 210) if is_landscape else (210, 297)
                    
                page = layout.Page(page_w, page_h, layout.Units.mm, margins=layout.Margins.all(5))
                
                # Copiar grabación maestra y recortar al encuadre de la hoja (con margen 0.5% para el grosor del borde)
                player_copy = master_backend.player().copy()
                pad_x = w * 0.005
                pad_y = h * 0.005
                player_copy.crop_rect(
                    (sheet_box.extmin.x - pad_x, sheet_box.extmin.y - pad_y),
                    (sheet_box.extmax.x + pad_x, sheet_box.extmax.y + pad_y),
                    min(w, h) * 0.001
                )
                backend = PyMuPdfBackend()
                backend.config = player_copy.config
                backend.background = player_copy.background
                backend.records = player_copy.records
                backend.properties = player_copy.properties
                
                try:
                    pdf_bytes = backend.get_pdf_bytes(page, settings=render_settings, render_box=sheet_box)
                    pdf_pages_bytes.append((f"Plano_{idx+1}", pdf_bytes))
                except ValueError as ve:
                    if "empty" in str(ve).lower() or "bounding box" in str(ve).lower():
                        print(f"Plano {idx+1} en el Espacio Modelo está vacío. Omitiendo.")
                        continue
                    else:
                        raise ve
                
        # Guardado de resultados
        if progress_callback:
            progress_callback("Guardando archivos PDF...", 0.9)
            
        combine_pdf = config.get("combine_pdf", True)
        
        output_files = []
        if combine_pdf:
            # Crear un único PDF combinado
            merged_pdf = pymupdf.open()
            for name, pdf_bytes in pdf_pages_bytes:
                page_doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
                merged_pdf.insert_pdf(page_doc)
                page_doc.close()
                
            merged_pdf.save(output_pdf_path)
            merged_pdf.close()
            output_files.append(output_pdf_path)
        else:
            # Guardar archivos individuales por cada página
            base_dir = os.path.dirname(output_pdf_path)
            base_name = os.path.splitext(os.path.basename(output_pdf_path))[0]
            
            for idx, (name, pdf_bytes) in enumerate(pdf_pages_bytes):
                file_path = os.path.join(base_dir, f"{base_name}_{name}.pdf")
                with open(file_path, "wb") as f:
                    f.write(pdf_bytes)
                output_files.append(file_path)
                
        if progress_callback:
            progress_callback("Conversión finalizada con éxito.", 1.0)
            
        return output_files
        
    finally:
        # Asegurar limpieza del archivo DXF temporal si fue creado exclusivamente para este proceso
        if not reuse_dxf and os.path.exists(temp_dxf_path):
            try:
                os.remove(temp_dxf_path)
            except Exception:
                pass

def get_preview_image(dxf_path, item_type, item_value, color_mode="color"):
    """
    Renderiza un layout o un encuadre de plano del Espacio Modelo
    a una imagen de vista previa en formato PIL Image.
    """
    import fitz
    from PIL import Image
    import io
    import math
    from ezdxf.addons.drawing import Frontend, RenderContext, layout
    from ezdxf.addons.drawing.pymupdf import PyMuPdfBackend
    from ezdxf.addons.drawing.config import Configuration, ColorPolicy, BackgroundPolicy
    
    doc = safe_read_dxf(dxf_path)
    ctx = RenderContext(doc)
    backend = PyMuPdfBackend()
    
    draw_config = Configuration(
        background_policy=BackgroundPolicy.WHITE,
        color_policy=ColorPolicy.BLACK if color_mode == "monochrome" else ColorPolicy.COLOR
    )
    
    if item_type == "layout":
        lay_obj = doc.layout(item_value)
        Frontend(ctx, backend, config=draw_config).draw_layout(lay_obj)
        try:
            page = layout.Page.from_dxf_layout(lay_obj)
            if page.width <= 1e-2 or page.height <= 1e-2 or math.isnan(page.width) or math.isnan(page.height):
                page = layout.Page(297, 210, layout.Units.mm)
        except Exception:
            page = layout.Page(297, 210, layout.Units.mm)
        pdf_bytes = backend.get_pdf_bytes(page)
    else:  # sheet (BoundingBox2d)
        msp = doc.modelspace()
        Frontend(ctx, backend, config=draw_config).draw_layout(msp)
        sheet_box = item_value
        w = sheet_box.extmax.x - sheet_box.extmin.x
        h = sheet_box.extmax.y - sheet_box.extmin.y
        is_landscape = w > h
        page_w, page_h = (297, 210) if is_landscape else (210, 297)
        page = layout.Page(page_w, page_h, layout.Units.mm, margins=layout.Margins.all(5))
        
        player_copy = backend.player().copy()
        pad_x = w * 0.005
        pad_y = h * 0.005
        player_copy.crop_rect(
            (sheet_box.extmin.x - pad_x, sheet_box.extmin.y - pad_y),
            (sheet_box.extmax.x + pad_x, sheet_box.extmax.y + pad_y),
            min(w, h) * 0.001
        )
        backend.records = player_copy.records
        pdf_bytes = backend.get_pdf_bytes(page, settings=layout.Settings(crop_at_margins=True), render_box=sheet_box)
        
    # Abrir el PDF generado en memoria y renderizarlo a imagen
    pdf_doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page_obj = pdf_doc.load_page(0)
    # Escalar a 2.0x (144 DPI) para obtener buena resolución
    pix = page_obj.get_pixmap(matrix=fitz.Matrix(2.0, 2.0))
    png_bytes = pix.tobytes("png")
    pdf_doc.close()
    
    # Cargar en PIL
    image = Image.open(io.BytesIO(png_bytes))
    return image
