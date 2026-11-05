import os
import sys
import threading
import queue
import time
import tempfile
import tkinter as tk
from tkinter import filedialog, messagebox
import customtkinter as ctk
import cad_processor

# Configuración inicial del tema y apariencia
ctk.set_appearance_mode("Dark")  # Forzar tema oscuro para máxima elegancia
ctk.set_default_color_theme("green")

class App(ctk.CTk):
    def __init__(self):
        super().__init__()

        # Configurar ventana
        self.title("PrintMyBlueprint - Conversor DWG a PDF Portable")
        self.geometry("1100x700")  # Aumentar ancho para el panel de selección de hojas
        self.minsize(950, 600)

        # Configurar icono de la ventana (para la barra de título y de tareas)
        try:
            icon_path = cad_processor.get_resource_path("app_icon.ico")
            if os.path.exists(icon_path):
                self.iconbitmap(icon_path)
        except Exception as e:
            print(f"No se pudo cargar el icono de la ventana: {e}")

        # Variables de estado
        self.dwg_path = ""
        self.output_dir = ""
        self.temp_dxf_path = ""
        self.pre_analysis_result = None
        self.queue = queue.Queue()
        self.processing = False
        
        # Elementos de la lista interactiva de planos
        self.sheet_checkbox_widgets = []
        self.sheet_items = []
        self.detected_sheets = []

        # Configurar Grid principal (1 columna lateral + 1 área de contenido)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._create_sidebar()
        self._create_main_content()

        # Vincular cierre de ventana para limpiar archivos temporales
        self.protocol("WM_DELETE_WINDOW", self._on_closing)

        # Iniciar bucle para chequear la cola de mensajes de hilos secundarios
        self.after(100, self._check_message_queue)

    def _create_sidebar(self):
        """Crea el panel lateral para carga de archivos e info rápida."""
        self.sidebar_frame = ctk.CTkFrame(self, width=280, corner_radius=0)
        self.sidebar_frame.grid(row=0, column=0, sticky="nsew")
        self.sidebar_frame.grid_rowconfigure(4, weight=1)  # Empujar elementos hacia arriba

        # Logotipo / Título
        self.logo_label = ctk.CTkLabel(
            self.sidebar_frame, 
            text="PrintMyBlueprint", 
            font=ctk.CTkFont(size=22, weight="bold", family="Segoe UI"),
            text_color="#2ecc71"
        )
        self.logo_label.grid(row=0, column=0, padx=20, pady=(25, 5))
        
        self.subtitle_label = ctk.CTkLabel(
            self.sidebar_frame, 
            text="DWG to PDF Splitter", 
            text_color="#888888",
            font=ctk.CTkFont(size=12, slant="italic")
        )
        self.subtitle_label.grid(row=1, column=0, padx=20, pady=(0, 25))

        # Botón de Selección de Archivo
        self.btn_select_file = ctk.CTkButton(
            self.sidebar_frame,
            text="Seleccionar DWG",
            command=self._on_select_file,
            height=40,
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color="#2ecc71",
            hover_color="#27ae60"
        )
        self.btn_select_file.grid(row=2, column=0, padx=20, pady=10)

        # Tarjeta de información del archivo seleccionado
        self.info_frame = ctk.CTkScrollableFrame(
            self.sidebar_frame, 
            label_text="Propiedades del Plano",
            label_font=ctk.CTkFont(size=12, weight="bold"),
            height=320
        )
        self.info_frame.grid(row=3, column=0, padx=20, pady=15, sticky="nsew")
        
        self.lbl_file_name = ctk.CTkLabel(
            self.info_frame, 
            text="Ningún archivo cargado", 
            wraplength=200, 
            justify="left",
            font=ctk.CTkFont(size=12)
        )
        self.lbl_file_name.pack(anchor="w", padx=5, pady=5)
        
        self.lbl_layouts_info = ctk.CTkLabel(self.info_frame, text="", justify="left", font=ctk.CTkFont(size=11))
        self.lbl_layouts_info.pack(anchor="w", padx=5, pady=2)

        self.lbl_blocks_info = ctk.CTkLabel(self.info_frame, text="", justify="left", font=ctk.CTkFont(size=11))
        self.lbl_blocks_info.pack(anchor="w", padx=5, pady=2)

        # Créditos inferiores
        self.credits_label = ctk.CTkLabel(
            self.sidebar_frame, 
            text="v1.1 - Portable App\nBasado en ezdxf + PyMuPDF", 
            text_color="#555555",
            font=ctk.CTkFont(size=10)
        )
        self.credits_label.grid(row=5, column=0, padx=20, pady=15)

    def _create_main_content(self):
        """Crea el panel principal con pestañas (Configuración y Visualizador)."""
        self.tabview = ctk.CTkTabview(self, corner_radius=15)
        self.tabview.grid(row=0, column=1, padx=20, pady=20, sticky="nsew")
        
        self.tabview.add("Configuración y Conversión")
        self.tabview.add("Visualizador de Planos")
        
        # El contenedor principal es la primera pestaña
        self.main_container = self.tabview.tab("Configuración y Conversión")
        self.main_container.grid_columnconfigure(0, weight=1)
        self.main_container.grid_rowconfigure(1, weight=1)
        
        # --- CONFIGURACIÓN TAB 2: VISUALIZADOR ---
        self.visualizer_tab = self.tabview.tab("Visualizador de Planos")
        self.visualizer_tab.grid_columnconfigure(0, weight=1)
        self.visualizer_tab.grid_rowconfigure(1, weight=1)
        
        # Barra superior de controles del visualizador
        self.viz_control_frame = ctk.CTkFrame(self.visualizer_tab, corner_radius=10)
        self.viz_control_frame.grid(row=0, column=0, padx=10, pady=10, sticky="ew")
        self.viz_control_frame.grid_columnconfigure(5, weight=1)
        
        self.lbl_preview_select = ctk.CTkLabel(self.viz_control_frame, text="Plano / Layout a ver:", font=ctk.CTkFont(size=12, weight="bold"))
        self.lbl_preview_select.grid(row=0, column=0, padx=15, pady=10, sticky="w")
        
        self.preview_items_map = {}
        self.opt_preview_select = ctk.CTkOptionMenu(
            self.viz_control_frame,
            values=["Carga un archivo..."],
            width=220,
            state="disabled"
        )
        self.opt_preview_select.grid(row=0, column=1, padx=10, pady=10, sticky="w")
        
        self.btn_load_preview = ctk.CTkButton(
            self.viz_control_frame,
            text="Ver Plano",
            command=self._on_load_preview,
            fg_color="#2ecc71",
            hover_color="#27ae60",
            font=ctk.CTkFont(size=12, weight="bold"),
            state="disabled",
            width=100
        )
        self.btn_load_preview.grid(row=0, column=2, padx=10, pady=10, sticky="w")

        # Color en el Visualizador
        self.lbl_viz_color = ctk.CTkLabel(self.viz_control_frame, text="Color:", font=ctk.CTkFont(size=12, weight="bold"))
        self.lbl_viz_color.grid(row=0, column=3, padx=(15, 5), pady=10, sticky="w")
        
        self.opt_viz_color_mode = ctk.CTkOptionMenu(
            self.viz_control_frame,
            values=["A Color", "Blanco y Negro (Monocromo)"],
            width=180,
            command=self._on_viz_color_change
        )
        self.opt_viz_color_mode.grid(row=0, column=4, padx=10, pady=10, sticky="w")
        
        self.lbl_preview_status = ctk.CTkLabel(self.viz_control_frame, text="Ningún plano cargado", text_color="#888888")
        self.lbl_preview_status.grid(row=0, column=5, padx=15, pady=10, sticky="e")
        
        # Contenedor de la Imagen
        self.preview_image_frame = ctk.CTkFrame(self.visualizer_tab, corner_radius=10, fg_color="#181818")
        self.preview_image_frame.grid(row=1, column=0, padx=10, pady=10, sticky="nsew")
        self.preview_image_frame.grid_columnconfigure(0, weight=1)
        self.preview_image_frame.grid_rowconfigure(0, weight=1)
        
        self.lbl_preview_canvas = ctk.CTkLabel(
            self.preview_image_frame, 
            text="Carga un archivo DWG, selecciona un plano y haz clic en 'Ver Plano'", 
            text_color="#888888"
        )
        self.lbl_preview_canvas.grid(row=0, column=0, padx=20, pady=20, sticky="nsew")

        # --- PANEL DE CONFIGURACIÓN ---
        self.config_frame = ctk.CTkFrame(self.main_container, corner_radius=10)
        self.config_frame.grid(row=0, column=0, padx=5, pady=5, sticky="ew")
        self.config_frame.grid_columnconfigure((0, 1, 2), weight=1) # Añadida tercera columna para el listado

        # Sección 1: Modo de Conversión
        self.mode_frame = ctk.CTkFrame(self.config_frame, fg_color="transparent")
        self.mode_frame.grid(row=0, column=0, padx=15, pady=15, sticky="nsew")
        
        self.lbl_mode_title = ctk.CTkLabel(
            self.mode_frame, 
            text="Origen de Planos", 
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color="#2ecc71"
        )
        self.lbl_mode_title.pack(anchor="w", pady=(0, 10))

        self.var_mode = tk.StringVar(value="modelspace")
        
        self.rad_mode_ms = ctk.CTkRadioButton(
            self.mode_frame, 
            text="Espacio Modelo (Model Space)", 
            variable=self.var_mode, 
            value="modelspace",
            command=self._on_mode_change
        )
        self.rad_mode_ms.pack(anchor="w", pady=5)
        
        self.rad_mode_lay = ctk.CTkRadioButton(
            self.mode_frame, 
            text="Pestañas de Presentación (Layouts)", 
            variable=self.var_mode, 
            value="layouts",
            command=self._on_mode_change
        )
        self.rad_mode_lay.pack(anchor="w", pady=5)

        # Sección 2: Configuración Espacio Modelo (Rótulos)
        self.ms_opt_frame = ctk.CTkFrame(self.config_frame, fg_color="transparent")
        self.ms_opt_frame.grid(row=0, column=1, padx=15, pady=15, sticky="nsew")
        
        self.lbl_ms_title = ctk.CTkLabel(
            self.ms_opt_frame, 
            text="Configuración Espacio Modelo", 
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color="#2ecc71"
        )
        self.lbl_ms_title.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        self.var_ms_method = tk.StringVar(value="auto")
        
        self.rad_method_auto = ctk.CTkRadioButton(
            self.ms_opt_frame, 
            text="Autodetectar rectángulos (A0-A4)", 
            variable=self.var_ms_method, 
            value="auto",
            command=self._on_ms_method_change
        )
        self.rad_method_auto.grid(row=1, column=0, columnspan=2, sticky="w", pady=4)

        self.rad_method_block = ctk.CTkRadioButton(
            self.ms_opt_frame, 
            text="Por Bloque de Rótulo:", 
            variable=self.var_ms_method, 
            value="block",
            command=self._on_ms_method_change
        )
        self.rad_method_block.grid(row=2, column=0, sticky="w", pady=4)
        
        self.opt_blocks = ctk.CTkOptionMenu(
            self.ms_opt_frame, 
            values=["Carga un archivo..."], 
            width=180,
            state="disabled",
            command=lambda _: self._update_detected_sheets_list()
        )
        self.opt_blocks.grid(row=2, column=1, padx=10, pady=4, sticky="w")

        self.rad_method_poly = ctk.CTkRadioButton(
            self.ms_opt_frame, 
            text="Por Capa de Polilínea:", 
            variable=self.var_ms_method, 
            value="polyline",
            command=self._on_ms_method_change
        )
        self.rad_method_poly.grid(row=3, column=0, sticky="w", pady=4)

        self.opt_layers = ctk.CTkOptionMenu(
            self.ms_opt_frame, 
            values=["Carga un archivo..."], 
            width=180,
            state="disabled",
            command=lambda _: self._update_detected_sheets_list()
        )
        self.opt_layers.grid(row=3, column=1, padx=10, pady=4, sticky="w")

        # Sección 3: Configuración de PDF y Salida
        self.output_frame = ctk.CTkFrame(self.config_frame, fg_color="transparent")
        self.output_frame.grid(row=1, column=0, columnspan=2, padx=15, pady=15, sticky="ew")
        
        self.lbl_out_title = ctk.CTkLabel(
            self.output_frame, 
            text="Ajustes de Impresión / PDF", 
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color="#2ecc71"
        )
        self.lbl_out_title.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))

        # Checkbox Combinar en un PDF
        self.var_combine = tk.BooleanVar(value=True)
        self.chk_combine = ctk.CTkCheckBox(
            self.output_frame, 
            text="Combinar en un solo archivo PDF multipágina", 
            variable=self.var_combine
        )
        self.chk_combine.grid(row=1, column=0, columnspan=2, sticky="w", pady=5)

        # Orientación
        self.lbl_orient = ctk.CTkLabel(self.output_frame, text="Orientación:")
        self.lbl_orient.grid(row=2, column=0, sticky="w", pady=5)
        
        self.var_orient = tk.StringVar(value="auto")
        self.opt_orient = ctk.CTkOptionMenu(
            self.output_frame, 
            values=["Auto (Según aspecto)", "Horizontal", "Vertical"],
            width=180
        )
        self.opt_orient.grid(row=2, column=1, padx=10, pady=5, sticky="w")

        # Carpeta Destino
        self.btn_select_dir = ctk.CTkButton(
            self.output_frame,
            text="Carpeta Destino",
            command=self._on_select_output_dir,
            height=28,
            width=130,
            fg_color="#2ecc71",
            hover_color="#27ae60"
        )
        self.btn_select_dir.grid(row=3, column=0, sticky="w", pady=5)
        
        self.lbl_output_dir = ctk.CTkLabel(
            self.output_frame, 
            text="Misma carpeta que el origen", 
            text_color="#888888",
            wraplength=350,
            justify="left"
        )
        self.lbl_output_dir.grid(row=3, column=1, columnspan=2, padx=10, sticky="w", pady=5)

        # Color de Líneas (Fondo Blanco forzado)
        self.lbl_color_mode = ctk.CTkLabel(self.output_frame, text="Color de Líneas:")
        self.lbl_color_mode.grid(row=4, column=0, sticky="w", pady=5)
        
        self.opt_color_mode = ctk.CTkOptionMenu(
            self.output_frame, 
            values=["A Color", "Blanco y Negro (Monocromo)"],
            width=180,
            command=self._on_settings_color_change
        )
        self.opt_color_mode.grid(row=4, column=1, padx=10, pady=5, sticky="w")

        # Sección 4: Panel derecho interactivo de selección de hojas (Checkboxes)
        self.sheets_sel_frame = ctk.CTkScrollableFrame(
            self.config_frame, 
            label_text="Planos / Layouts Detectados",
            label_font=ctk.CTkFont(size=14, weight="bold"),
            height=260
        )
        self.sheets_sel_frame.grid(row=0, column=2, rowspan=2, padx=15, pady=15, sticky="nsew")
        
        self.lbl_no_sheets = ctk.CTkLabel(
            self.sheets_sel_frame, 
            text="Carga un archivo DWG...", 
            text_color="#888888"
        )
        self.lbl_no_sheets.pack(pady=30)

        # --- ÁREA DE CONVERSIÓN Y BITÁCORA ---
        self.action_frame = ctk.CTkFrame(self.main_container, corner_radius=10)
        self.action_frame.grid(row=1, column=0, padx=5, pady=5, sticky="nsew")
        self.action_frame.grid_columnconfigure(0, weight=1)
        self.action_frame.grid_rowconfigure(2, weight=1) # El log se estira

        # Botón Ejecutar
        self.btn_convert = ctk.CTkButton(
            self.action_frame,
            text="CONVERTIR A PDF",
            command=self._on_convert,
            height=45,
            fg_color="#2ecc71",     # Botón verde esmeralda
            hover_color="#27ae60",  # Verde oscuro al pasar cursor
            font=ctk.CTkFont(size=15, weight="bold")
        )
        self.btn_convert.grid(row=0, column=0, padx=20, pady=(15, 10), sticky="ew")

        # Barra de Progreso
        self.progress_bar = ctk.CTkProgressBar(self.action_frame, progress_color="#2ecc71")
        self.progress_bar.grid(row=1, column=0, padx=20, pady=5, sticky="ew")
        self.progress_bar.set(0)

        # Caja de registros (Log)
        self.log_textbox = ctk.CTkTextbox(
            self.action_frame, 
            font=ctk.CTkFont(family="Consolas", size=11),
            fg_color="#181818"
        )
        self.log_textbox.grid(row=2, column=0, padx=20, pady=(10, 20), sticky="nsew")
        self.log_textbox.insert("0.0", ">>> Consola de Procesamiento iniciada...\n")
        self.log_textbox.configure(state="disabled")

    # --- MANEJADORES DE EVENTOS ---
    
    def _on_select_file(self):
        file_path = filedialog.askopenfilename(
            title="Seleccionar Archivo DWG",
            filetypes=[("Archivos AutoCAD DWG", "*.dwg"), ("Todos los archivos", "*.*")]
        )
        if not file_path:
            return
            
        self.dwg_path = file_path
        self.lbl_file_name.configure(text=f"Archivo: {os.path.basename(file_path)}")
        self._log(f"\nArchivo DWG seleccionado:\n{file_path}\nIniciando pre-análisis de estructuras...")
        
        # Eliminar archivo temporal anterior si existe
        if self.temp_dxf_path and os.path.exists(self.temp_dxf_path):
            try:
                os.remove(self.temp_dxf_path)
            except Exception:
                pass
                
        # Crear un nombre temporal único y seguro (solo ASCII) usando PID y timestamp
        temp_dir = tempfile.gettempdir()
        self.temp_dxf_path = os.path.join(temp_dir, f"temp_blueprint_pre_{os.getpid()}_{int(time.time())}.dxf")
        
        # Desactivar botones durante el análisis
        self.btn_select_file.configure(state="disabled")
        self.btn_convert.configure(state="disabled")
        
        # Correr pre-análisis en un hilo secundario
        threading.Thread(
            target=self._async_pre_analyze, 
            args=(file_path, self.temp_dxf_path), 
            daemon=True
        ).start()

    def _async_pre_analyze(self, file_path, temp_dxf_path):
        try:
            self.queue.put(("log", "Extrayendo geometría del DWG temporalmente..."))
            cad_processor.convert_dwg_to_dxf(file_path, temp_dxf_path)
            
            self.queue.put(("log", "Analizando capas, bloques y pestañas de presentación..."))
            result = cad_processor.pre_analyze_dxf(temp_dxf_path)
            
            # Mantenemos el DXF temporal activo para escaneos interactivos del Espacio Modelo
            self.queue.put(("pre_analyze_success", result))
        except Exception as e:
            self.queue.put(("pre_analyze_error", str(e)))

    def _on_select_output_dir(self):
        selected = filedialog.askdirectory(title="Seleccionar carpeta de destino")
        if selected:
            self.output_dir = selected
            self.lbl_output_dir.configure(text=selected, text_color="#2eb85c")
            self._log(f"Directorio de destino establecido en: {selected}")

    def _on_mode_change(self):
        mode = self.var_mode.get()
        if mode == "layouts":
            self.rad_method_auto.configure(state="disabled")
            self.rad_method_block.configure(state="disabled")
            self.rad_method_poly.configure(state="disabled")
            self.opt_blocks.configure(state="disabled")
            self.opt_layers.configure(state="disabled")
            self._log("Modo cambiado a: Pestañas de Presentación (Layouts).")
        else:
            self.rad_method_auto.configure(state="normal")
            self.rad_method_block.configure(state="normal")
            self.rad_method_poly.configure(state="normal")
            self._on_ms_method_change()
            self._log("Modo cambiado a: Espacio Modelo (Model Space).")
            
        self._update_detected_sheets_list()

    def _on_ms_method_change(self):
        method = self.var_ms_method.get()
        if method == "auto":
            self.opt_blocks.configure(state="disabled")
            self.opt_layers.configure(state="disabled")
            self._log("Método: Detección Automática de formatos A0-A4.")
        elif method == "block":
            self.opt_blocks.configure(state="normal")
            self.opt_layers.configure(state="disabled")
            self._log("Método: Detección por Bloque de Rótulo.")
        elif method == "polyline":
            self.opt_blocks.configure(state="disabled")
            self.opt_layers.configure(state="normal")
            self._log("Método: Detección por Polilínea de Capa.")
            
        self._update_detected_sheets_list()

    # --- LISTA INTERACTIVA DE HOJAS ---

    def _update_detected_sheets_list(self):
        """Actualiza en tiempo real los checkboxes de planos en base a los criterios seleccionados."""
        # Limpiar listado anterior
        for widget in self.sheet_checkbox_widgets:
            widget.destroy()
        self.sheet_checkbox_widgets = []
        self.sheet_items = []
        
        # Eliminar etiqueta por defecto
        if hasattr(self, "lbl_no_sheets") and self.lbl_no_sheets.winfo_exists():
            self.lbl_no_sheets.destroy()
            
        if not self.dwg_path or not self.pre_analysis_result:
            self.lbl_no_sheets = ctk.CTkLabel(self.sheets_sel_frame, text="Carga un archivo DWG...", text_color="#888888")
            self.lbl_no_sheets.pack(pady=30)
            return

        mode = self.var_mode.get()
        
        if mode == "layouts":
            # Listar todas las pestañas de presentación
            layouts = self.pre_analysis_result.get("layouts", [])
            if not layouts:
                self.lbl_no_sheets = ctk.CTkLabel(self.sheets_sel_frame, text="Sin layouts válidos", text_color="#888888")
                self.lbl_no_sheets.pack(pady=30)
                return
                
            for lay_name in layouts:
                chk = ctk.CTkCheckBox(self.sheets_sel_frame, text=f"Layout: {lay_name}")
                chk.pack(anchor="w", padx=15, pady=6)
                chk.select()  # Checked por defecto
                self.sheet_checkbox_widgets.append(chk)
                self.sheet_items.append(("layout", lay_name))
                
        else:  # modelspace
            # Realizar detección usando la configuración actual en el DXF temporal
            if not self.temp_dxf_path or not os.path.exists(self.temp_dxf_path):
                self.lbl_no_sheets = ctk.CTkLabel(self.sheets_sel_frame, text="Error: Temp DXF ausente", text_color="#ff5555")
                self.lbl_no_sheets.pack(pady=30)
                return
                
            method = self.var_ms_method.get()
            
            # Extraer nombre limpio de bloque
            block_name = self.opt_blocks.get() if method == "block" else None
            if block_name and " (" in block_name:
                block_name = block_name.split(" (")[0]
                
            layer_name = self.opt_layers.get() if method == "polyline" else None
            
            try:
                # Buscar planos en espacio modelo
                self.detected_sheets = cad_processor.detect_sheets_in_modelspace(
                    self.temp_dxf_path,
                    method=method,
                    block_name=block_name,
                    layer_name=layer_name
                )
                
                if not self.detected_sheets:
                    self.lbl_no_sheets = ctk.CTkLabel(self.sheets_sel_frame, text="No se detectó ningún plano", text_color="#888888")
                    self.lbl_no_sheets.pack(pady=30)
                    return
                    
                for idx, bbox in enumerate(self.detected_sheets):
                    w = int(bbox.extmax.x - bbox.extmin.x)
                    h = int(bbox.extmax.y - bbox.extmin.y)
                    x = int(bbox.extmin.x)
                    y = int(bbox.extmin.y)
                    label_text = f"Plano {idx+1} ({w}x{h}) en ({x},{y})"
                    
                    chk = ctk.CTkCheckBox(self.sheets_sel_frame, text=label_text)
                    chk.pack(anchor="w", padx=15, pady=6)
                    chk.select()  # Checked por defecto
                    self.sheet_checkbox_widgets.append(chk)
                    self.sheet_items.append(("sheet", bbox))
                    
            except Exception as e:
                self.lbl_no_sheets = ctk.CTkLabel(self.sheets_sel_frame, text=f"Error al escanear:\n{e}", text_color="#ff5555")
                self.lbl_no_sheets.pack(pady=20)

        # Actualizar desplegable del visualizador dinámicamente
        preview_options = []
        self.preview_items_map = {}
        for idx, (item_type, item_val) in enumerate(self.sheet_items):
            if item_type == "layout":
                name = f"Layout: {item_val}"
            else:
                w = int(item_val.extmax.x - item_val.extmin.x)
                h = int(item_val.extmax.y - item_val.extmin.y)
                name = f"Plano {idx+1} ({w}x{h})"
            preview_options.append(name)
            self.preview_items_map[name] = (item_type, item_val)
            
        if preview_options:
            self.opt_preview_select.configure(values=preview_options, state="normal")
            self.opt_preview_select.set(preview_options[0])
            self.btn_load_preview.configure(state="normal")
        else:
            self.opt_preview_select.configure(values=["Carga un archivo..."], state="disabled")
            self.opt_preview_select.set("Carga un archivo...")
            self.btn_load_preview.configure(state="disabled")

    # --- INICIO DE CONVERSIÓN ---

    def _on_convert(self):
        if not self.dwg_path:
            self._log("ERROR: Debes seleccionar un archivo DWG primero.")
            messagebox.showwarning(title="Falta Archivo", message="Por favor, selecciona un archivo .dwg")
            return
            
        # Extraer elementos marcados
        selected_layouts = []
        selected_sheets = []
        
        for chk, (item_type, item_val) in zip(self.sheet_checkbox_widgets, self.sheet_items):
            if chk.get() == 1:  # Checkbox activado
                if item_type == "layout":
                    selected_layouts.append(item_val)
                elif item_type == "sheet":
                    selected_sheets.append(item_val)
                    
        # Verificar que se seleccionara al menos uno
        mode = self.var_mode.get()
        if mode == "layouts" and not selected_layouts:
            messagebox.showwarning("Sin Selección", "Marca al menos un Layout para convertir.")
            return
        if mode == "modelspace" and not selected_sheets:
            messagebox.showwarning("Sin Selección", "Marca al menos un plano en el Espacio Modelo para convertir.")
            return

        # Preparar configuración
        color_val = self.opt_color_mode.get()
        config = {
            "mode": mode,
            "combine_pdf": self.var_combine.get(),
            "orientation": self.opt_orient.get(),
            "color_mode": "monochrome" if "Blanco y Negro" in color_val else "color",
            "selected_layouts": selected_layouts if mode == "layouts" else None,
            "selected_sheets": selected_sheets if mode == "modelspace" else None,
            "temp_dxf_path": self.temp_dxf_path
        }

        # Traducir orientación
        orient_map = {
            "Auto (Según aspecto)": "auto",
            "Horizontal": "landscape",
            "Vertical": "portrait"
        }
        config["orientation"] = orient_map.get(config["orientation"], "auto")

        # Ruta del PDF final
        base_dir = self.output_dir if self.output_dir else os.path.dirname(self.dwg_path)
        base_name = os.path.splitext(os.path.basename(self.dwg_path))[0]
        output_pdf_path = os.path.join(base_dir, f"{base_name}_Planos.pdf")

        # Bloquear controles
        self.processing = True
        self.btn_select_file.configure(state="disabled")
        self.btn_convert.configure(state="disabled", text="PROCESANDO...")
        self.progress_bar.set(0)

        # Correr proceso en hilo secundario
        threading.Thread(
            target=self._async_convert, 
            args=(self.dwg_path, output_pdf_path, config), 
            daemon=True
        ).start()

    def _async_convert(self, dwg_path, output_pdf_path, config):
        def progress_cb(text, value):
            self.queue.put(("progress", (text, value)))
            
        try:
            output_files = cad_processor.process_file_to_pdf(
                dwg_path, 
                output_pdf_path, 
                config, 
                progress_callback=progress_cb
            )
            self.queue.put(("convert_success", output_files))
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            self.queue.put(("convert_error", f"{e}\n\nTraceback:\n{tb}"))

    # --- GESTIÓN DE COLA DE MENSAJES ---

    def _check_message_queue(self):
        """Revisa la cola periódicamente para actualizar la UI desde hilos secundarios."""
        try:
            while True:
                msg_type, data = self.queue.get_nowait()
                
                if msg_type == "log":
                    self._log(data)
                    
                elif msg_type == "progress":
                    text, value = data
                    self._log(text)
                    self.progress_bar.set(value)
                    
                elif msg_type == "pre_analyze_success":
                    self.pre_analysis_result = data
                    self._handle_pre_analysis_success()
                    
                elif msg_type == "pre_analyze_error":
                    self._log(f"ERROR en pre-análisis:\n{data}")
                    self.btn_select_file.configure(state="normal")
                    
                elif msg_type == "convert_success":
                    self.progress_bar.set(1.0)
                    self._log("\n¡PROCESO FINALIZADO CON ÉXITO!")
                    self._log("Archivos generados:")
                    for f in data:
                        self._log(f"- {f}")
                    self._reset_ui_after_process()
                    messagebox.showinfo("Proceso Completo", "Se convirtieron y dividieron los planos con éxito.")
                    
                elif msg_type == "convert_error":
                    self._log(f"\nERROR DURANTE LA CONVERSIÓN:\n{data}")
                    self.progress_bar.set(0)
                    self._reset_ui_after_process()
                    if "Todas las pestañas de presentación" in data:
                        messagebox.showwarning(
                            "Presentaciones Vacías", 
                            "El archivo DWG no contiene dibujos en las pestañas de presentación (Layouts).\n\n"
                            "Por favor, cambia el 'Origen de Planos' a 'Espacio Modelo (Model Space)' en la parte superior e inténtalo de nuevo."
                        )
                    else:
                        messagebox.showerror("Error", f"Ocurrió un error al convertir:\n{data}")
                        
                elif msg_type == "preview_success":
                    self.lbl_preview_status.configure(text="Vista previa cargada", text_color="#2ecc71")
                    self.btn_load_preview.configure(state="normal")
                    self._display_preview_image(data)
                    
                elif msg_type == "preview_error":
                    self.lbl_preview_status.configure(text="Plano vacío o no ploteable", text_color="#ffbb00")
                    self.btn_load_preview.configure(state="normal")
                    self.lbl_preview_canvas.configure(image="", text="Este plano/layout no contiene elementos visibles para impresión (está vacío).")
                    
                self.queue.task_done()
        except queue.Empty:
            pass
        finally:
            self.after(100, self._check_message_queue)

    def _handle_pre_analysis_success(self):
        res = self.pre_analysis_result
        self._log("Pre-análisis finalizado correctamente.")
        
        # Actualizar información rápida en el panel lateral
        layouts_count = len(res["layouts"])
        blocks_count = len(res["blocks"])
        
        self.lbl_layouts_info.configure(text=f"Presentaciones (Layouts): {layouts_count}")
        self.lbl_blocks_info.configure(text=f"Bloques en Mod. Space: {blocks_count}")
        
        # Activar/Desactivar pestaña Layouts según si existen
        if layouts_count > 0:
            self.rad_mode_lay.configure(state="normal")
        else:
            self.rad_mode_lay.configure(state="disabled")
            self._log("Nota: No se detectaron layouts de presentación (Layouts).")

        # Por defecto forzar el Espacio Modelo ya que es donde comúnmente están los planos
        self.var_mode.set("modelspace")
        self.rad_mode_ms.select()

        # Poblar desplegable de bloques candidatos
        block_names = [f"{b[0]} ({b[1]} u.)" for b in res["blocks"]]
        if block_names:
            self.opt_blocks.configure(values=block_names)
            self.opt_blocks.set(block_names[0])
        else:
            self.opt_blocks.configure(values=["Sin bloques encontrados"])
            self.opt_blocks.set("Sin bloques")

        # Poblar desplegable de capas
        layers_names = res["layers"]
        if layers_names:
            self.opt_layers.configure(values=layers_names)
            self.opt_layers.set(layers_names[0])
        else:
            self.opt_layers.configure(values=["Sin capas"])
            self.opt_layers.set("Sin capas")

        # Configurar estado inicial de componentes del Espacio Modelo
        self._on_mode_change()

        # Restaurar botones
        self.btn_select_file.configure(state="normal")
        self.btn_convert.configure(state="normal")

    def _reset_ui_after_process(self):
        self.processing = False
        self.btn_select_file.configure(state="normal")
        self.btn_convert.configure(state="normal", text="CONVERTIR A PDF")

    def _log(self, text):
        """Agrega texto de forma segura al control de textbox del log."""
        self.log_textbox.configure(state="normal")
        self.log_textbox.insert("end", f"{text}\n")
        self.log_textbox.see("end")
        self.log_textbox.configure(state="disabled")

    def _on_closing(self):
        # Limpiar archivo temporal al cerrar
        if self.temp_dxf_path and os.path.exists(self.temp_dxf_path):
            try:
                os.remove(self.temp_dxf_path)
            except Exception:
                pass
        self.destroy()

    # --- MÉTODOS DEL VISUALIZADOR ---

    def _on_load_preview(self):
        selected_name = self.opt_preview_select.get()
        if not selected_name or selected_name == "Carga un archivo...":
            return
            
        item_type, item_val = self.preview_items_map.get(selected_name, (None, None))
        if not item_type:
            return
            
        self.lbl_preview_status.configure(text="Generando vista previa...", text_color="#2ecc71")
        self.btn_load_preview.configure(state="disabled")
        
        # Determinar modo de color seleccionado
        color_val = self.opt_color_mode.get()
        color_mode = "monochrome" if "Blanco y Negro" in color_val else "color"
        
        # Correr renderizado en hilo secundario
        threading.Thread(
            target=self._async_load_preview,
            args=(self.temp_dxf_path, item_type, item_val, color_mode),
            daemon=True
        ).start()

    def _async_load_preview(self, dxf_path, item_type, item_val, color_mode):
        try:
            image = cad_processor.get_preview_image(dxf_path, item_type, item_val, color_mode)
            self.queue.put(("preview_success", image))
        except Exception as e:
            self.queue.put(("preview_error", str(e)))

    def _on_viz_color_change(self, val):
        self.opt_color_mode.set(val)
        self._update_detected_sheets_list()
        
    def _on_settings_color_change(self, val):
        self.opt_viz_color_mode.set(val)
        self._update_detected_sheets_list()

    def _display_preview_image(self, pil_image):
        from PIL import Image
        
        # Obtener dimensiones máximas para ajustar al contenedor de la UI
        display_w = 750
        display_h = 480
        
        img = pil_image.copy()
        img.thumbnail((display_w, display_h), Image.Resampling.LANCZOS)
        
        # Convertir a CTkImage para CustomTkinter
        self.ctk_preview_image = ctk.CTkImage(
            light_image=img,
            dark_image=img,
            size=(img.width, img.height)
        )
        
        # Mostrar en el Label centrado
        self.lbl_preview_canvas.configure(image=self.ctk_preview_image, text="")

if __name__ == "__main__":
    app = App()
    app.mainloop()
