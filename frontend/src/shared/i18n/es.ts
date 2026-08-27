/**
 * Spanish. Keys are the English source text.
 *
 * Not a literal translation, and the difference is the point. "found nothing" is "sin
 * resultados", not "encontró nada"; "Choose PDFs" is "Elige PDFs", not "Escoge documentos
 * PDF". The register is one professional talking to another about their own archive —
 * "tú", never "usted", because the product is a tool they use all day and not a form they
 * are filling in for an institution.
 *
 * Untranslated on purpose: document names, label names and organisation names, which are
 * the customer's own data; and identifiers a reader would search for verbatim — "Form 941",
 * article numbers, model names. The generation prompt already applies the same rule to
 * answers, so the interface and the model agree about what stays in the original.
 */
import type { Catalogue } from "./index";

export const es: Catalogue = {
  // --- shell and navigation -----------------------------------------------------------
  "Ask your documents": "Pregunta a tus documentos",
  Search: "Buscar",
  Recent: "Recientes",
  Ingestion: "Procesamiento",
  Status: "Estado",
  "document ready": "documento listo",
  "documents ready": "documentos listos",
  Chat: "Chat",
  Folders: "Carpetas",
  Upload: "Subir",
  "Uploading…": "Subiendo…",
  History: "Historial",
  Admin: "Administración",
  System: "Sistema",
  Profile: "Perfil",
  Theme: "Tema",
  Language: "Idioma",
  Auto: "Auto",
  Light: "Claro",
  Dark: "Oscuro",
  "Collapse sidebar": "Plegar la barra lateral",
  "Expand sidebar": "Desplegar la barra lateral",
  "Close document preview": "Cerrar la vista del documento",
  "Opening the document…": "Abriendo el documento…",
  Cancel: "Cancelar",
  Save: "Guardar",
  Done: "Hecho",
  Delete: "Eliminar",
  "Load more": "Cargar más",
  "Loading…": "Cargando…",
  Question: "Pregunta",

  // --- sign in ------------------------------------------------------------------------
  "Zenith Enterprise Document Intelligence Platform":
    "Zenith Enterprise · Plataforma de inteligencia documental",
  "Work email": "Correo de trabajo",
  Password: "Contraseña",
  "Sign In to Workspace": "Entrar",
  "Signing in…": "Entrando…",
  "No account? Your workspace administrator creates one for you.":
    "¿Sin cuenta? La crea el administrador de tu organización.",
  "RLS Security Enforced": "Aislamiento por RLS",
  "Multi-Tenant Isolated": "Multi-organización",

  // --- search -------------------------------------------------------------------------
  "Search your corpus": "Busca en tu corpus",
  "Search passages by keyword and meaning": "Busca pasajes por palabra y por significado",
  "Keyword and meaning at once — passages come back ranked, with the page they came from. Nothing is generated here; use Chat for a written answer.":
    "Palabra y significado a la vez: los pasajes vuelven ordenados y con la página de la que salen. Aquí no se genera nada; para una respuesta redactada usa el Chat.",
  Clear: "Limpiar",
  Stop: "Parar",
  "Try again": "Reintentar",
  "Nothing matched that query.": "Ninguna coincidencia.",
  "There are no documents to search yet. Upload one to get started.":
    "Todavía no hay documentos que buscar. Sube uno para empezar.",
  "This search only looked inside": "Esta búsqueda sólo miró dentro de",
  "Search everything instead": "Buscar en todo",
  "· Try fewer words — every one of them has to appear.":
    "· Prueba con menos palabras: tienen que aparecer todas.",
  "· Try the wording the document itself would use.":
    "· Prueba con las palabras que usaría el propio documento.",
  "· Ask it as a question in Chat, which reads the passages for you.":
    "· Pregúntalo en el Chat, que lee los pasajes por ti.",
  "keyword —": "palabra —",
  "meaning —": "significado —",

  // --- chat ---------------------------------------------------------------------------
  "Answered only from what is in your corpus — never from what the model happens to know.":
    "Responde sólo con lo que hay en tu corpus, nunca con lo que el modelo sepa por su cuenta.",
  "Ask a question — @ to answer from one document":
    "Escribe una pregunta — @ para responder desde un solo documento",
  Send: "Enviar",
  "Stop generating": "Parar",
  "Asked here": "Preguntado aquí",
  Try: "Prueba",
  "Answering from": "Respondiendo desde",
  only: "sólo",
  "No answer was found in your documents.": "No hay respuesta en tus documentos.",
  Documents: "Documentos",
  "No documents match that.": "Ningún documento coincide.",
  "{count} cited": { one: "{count} cita", other: "{count} citas" },
  "found nothing": "sin resultados",

  // --- upload -------------------------------------------------------------------------
  "Upload PDFs": "Subir PDFs",
  "Choose PDFs": "Elige PDFs",
  "or drop them here": "o suéltalos aquí",
  "Drop several to tag them together before anything is sent.":
    "Suelta varios para etiquetarlos juntos antes de enviar nada.",
  "Searches run more slowly while a document is being processed.":
    "Las búsquedas van más lentas mientras se procesa un documento.",
  "Searches run more slowly while this is happening.":
    "Las búsquedas van más lentas mientras esto ocurre.",
  "Nothing being ingested": "No hay nada procesándose",
  Name: "Nombre",
  Description: "Descripción",
  "(optional)": "(opcional)",
  "(optional — defaults to the file's own name)":
    "(opcional — si lo dejas vacío, se usa el nombre del fichero)",
  "What is this document, or why does it matter?": "¿Qué es este documento, o por qué importa?",
  "Cancel remaining": "Cancelar lo que queda",
  "Auto-tag": "Etiquetar solo",
  "Tag selected": "Etiquetar la selección",
  "Select all staged files": "Seleccionar todos los ficheros preparados",
  "Suggest tags from each file's opening pages, using the configured model":
    "Sugerir etiquetas leyendo las primeras páginas de cada fichero con el modelo configurado",

  // --- labels -------------------------------------------------------------------------
  Labels: "Etiquetas",
  "Recently used": "Uso reciente",
  "Most used": "Más usadas",
  Newest: "Más recientes",
  "Command palette": "Paleta de comandos",
  Selected: "Seleccionadas",
  "File under": "Archivar en",
  "(no label — visible tenant-wide)":
    "(sin etiqueta — visible para toda la organización)",
  "Search labels": "Buscar etiquetas",
  "Sort labels": "Ordenar etiquetas",
  "Sort by": "Ordenar por",
  "New label": "Nueva etiqueta",
  "New label name": "Nombre de la nueva etiqueta",
  "Label name": "Nombre de la etiqueta",
  "In use": "En uso",
  Mine: "Mías",
  "Searching…": "Buscando…",
  "Merge labels": "Fusionar etiquetas",
  Keep: "Conservar",
  default: "por defecto",

  // --- documents ----------------------------------------------------------------------
  "All documents": "Todos los documentos",
  "Loading documents…": "Cargando documentos…",
  "Loading folders…": "Cargando carpetas…",
  "No folders yet — they appear as soon as an uploaded document carries a label.":
    "Aún no hay carpetas: aparecen en cuanto un documento subido lleva una etiqueta.",
  "still being processed": "en proceso",
  "failed to process": "falló al procesar",
  "Delete this document?": "¿Eliminar este documento?",
  "Delete document": "Eliminar documento",
  "Who can open this": "Quién puede abrirlo",
  "Readable through": "Se lee a través de",
  "No labels you can see. Ask an administrator if this looks wrong.":
    "Ninguna etiqueta que tú puedas ver. Pregunta a un administrador si esto no cuadra.",
  "Working out who can open this…": "Calculando quién puede abrirlo…",
  "No group opens this label.": "Ningún grupo abre esta etiqueta.",

  // --- viewer -------------------------------------------------------------------------
  "Opening…": "Abriendo…",
  "← Previous": "← Anterior",
  "Next →": "Siguiente →",
  "Back to citation": "Volver a la cita",
  "Click a citation in an answer to open the page it came from.":
    "Pulsa una cita de una respuesta para abrir la página de la que salió.",

  // --- status -------------------------------------------------------------------------
  Hardware: "Hardware",
  Answers: "Respuestas",
  Reranking: "Reordenación",
  passages: "pasajes",
  "Nothing to search yet.": "Todavía no hay nada que buscar.",
  "no model configured": "sin modelo configurado",
  "off for this hardware": "desactivado en este hardware",

  // --- history ------------------------------------------------------------------------
  "Search your questions": "Busca en tus preguntas",
  "Only mine": "Sólo mías",
  "Found nothing": "Sin resultados",

  // --- admin --------------------------------------------------------------------------
  "People and groups": "Personas y grupos",
  "Access matrix": "Matriz de acceso",
  "Access record": "Registro de acceso",
  "Audit log": "Registro de auditoría",
  Analytics: "Analítica",
  Roles: "Roles",
  Groups: "Grupos",
  "Answer model": "Modelo de respuesta",
  "Invite a colleague": "Invitar a alguien",
  Email: "Correo",
  Role: "Rol",
  Invite: "Invitar",
  "Copy link": "Copiar enlace",
  none: "ninguno",
  "Add group": "Añadir grupo",
  "Finance, Engineering, Project-Alpha…": "Finanzas, Ingeniería, Proyecto-Alfa…",
  "Loading groups…": "Cargando grupos…",
  "Loading people…": "Cargando personas…",
  "No label matches that.": "Ninguna etiqueta coincide.",
  "no clearance": "sin habilitación",
  saved: "guardado",
  clearance: "habilitación",
  "built in — not editable": "de serie — no editable",
  "No groups yet. Create one above, then come back to put people in it.":
    "Aún no hay grupos. Crea uno arriba y vuelve para meter gente en él.",
  "In no group — reaches documents only through labels granted to their roles.":
    "Sin grupo: llega a los documentos sólo por las etiquetas concedidas a sus roles.",
  "Nothing has changed access yet. Grants, group edits and clearance changes appear here.":
    "Todavía no ha cambiado ningún acceso. Aquí aparecen las concesiones, los cambios de grupo y los de habilitación.",
  "Reading the query log…": "Leyendo el registro de consultas…",
  "No questions in this window.": "No hay preguntas en este periodo.",
  "Most active": "Más activos",
  "Most cited documents": "Documentos más citados",
  Who: "Quién",
  When: "Cuándo",
  Read: "Leídos",
  total: "total",
  "This provider does not report token usage, so there is no cost to show.":
    "Este proveedor no informa del consumo de tokens, así que no hay coste que mostrar.",
  Endpoint: "Endpoint",
  Model: "Modelo",
  "API key": "Clave de API",

  // --- profile ------------------------------------------------------------------------
  Account: "Cuenta",
  Access: "Acceso",
  Sessions: "Sesiones",
  "Your name": "Tu nombre",
  Unnamed: "Sin nombre",
  "Current password": "Contraseña actual",
  "New password": "Contraseña nueva",
  "(at least 8 characters)": "(al menos 8 caracteres)",
  "Checking the link…": "Comprobando el enlace…",
  "This link no longer works": "Este enlace ya no funciona",
  "Setting the password for": "Estableciendo la contraseña de",
  "Your password is set": "Contraseña establecida",
  "You can sign in now.": "Ya puedes entrar.",
  "Go to sign in": "Ir a la entrada",

  // --- system -------------------------------------------------------------------------
  "Loading organisations…": "Cargando organizaciones…",
  "No organisations yet.": "Aún no hay organizaciones.",
  "New organisation": "Nueva organización",
  "Organisation name": "Nombre de la organización",
  "First administrator's email": "Correo del primer administrador",
  Suspended: "Suspendida",

  // --- command palette ----------------------------------------------------------------
  "Go to a screen, find a document, or repeat a question…":
    "Ve a una pantalla, busca un documento o repite una pregunta…",
};
