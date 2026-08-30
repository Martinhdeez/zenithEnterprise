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
  "document ready": "documento listo",
  "documents ready": "documentos listos",
  Chat: "Chat",
  Folders: "Carpetas",
  Upload: "Subir",
  "Uploading…": "Subiendo…",
  Queued: "En cola",
  "Reading the document": "Leyendo el documento",
  "Splitting into passages": "Dividiendo en pasajes",
  "Building the index": "Construyendo el índice",
  "Filing the document": "Archivando el documento",
  Processing: "Procesando",
  "Still processing": "Aún procesándose",
  Failed: "Falló",
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
  "Manage": "Gestión",
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
  "Keyword and meaning at once — passages come back ranked, with the page they came from. Nothing is generated here: open a result and ask about that document for a written answer.":
    "Palabra y significado a la vez: los pasajes vuelven ordenados y con la página de la que salen. Aquí no se genera nada: abre un resultado y pregunta sobre ese documento para obtener una respuesta redactada.",
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
  "Go to": "Ir a",
  "Ask again": "Volver a preguntar",
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
  "Previous page": "Página anterior",
  "Next page": "Página siguiente",
  "Page {page}": "Página {page}",
  "page {page}": "página {page}",
  "Page {page} — cited page {cited}": "Página {page} — la cita está en la {cited}",
  "Back to citation": "Volver a la cita",
  "Zoom in": "Acercar",
  "Zoom out": "Alejar",
  "Clear {name}": "Limpiar {name}",
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
  "Exit fullscreen": "Salir de pantalla completa",
  "Fullscreen": "Pantalla completa",
  "Saving…": "Guardando…",
  "That change was not saved.": "No se guardó el cambio.",
  "That group was not created.": "No se creó el grupo.",
  "The audit log did not load.": "No se pudo cargar el registro de auditoría.",
  "Automatic classification": "Clasificación automática",
  "The record could not be loaded.": "No se pudo cargar el registro.",
  "You do not have permission to read the access record.": "No tienes permiso para leer el registro de acceso.",
  "Issuing…": "Emitiendo…",
  "Reset link": "Enlace de restablecimiento",
  "That reset link could not be issued.": "No se pudo emitir el enlace.",
  "The invitation failed.": "Falló la invitación.",
  "That could not be saved.": "No se pudo guardar.",
  "Creating…": "Creando…",
  "New role": "Nuevo rol",
  "Roles could not be loaded.": "No se pudieron cargar los roles.",
  "That change was refused.": "Se rechazó el cambio.",
  "Hide password": "Ocultar contraseña",
  "Show password": "Mostrar contraseña",
  "Sign in failed.": "No se pudo entrar.",
  "Change password": "Cambiar contraseña",
  "Changing…": "Cambiando…",
  "Ending…": "Cerrando…",
  "Sign out everywhere": "Cerrar sesión en todas partes",
  "Choose a new password": "Elige una contraseña nueva",
  "Set password": "Establecer contraseña",
  "Setting…": "Estableciendo…",
  "The password could not be set.": "No se pudo establecer la contraseña.",
  "Welcome to Zenith": "Te damos la bienvenida a Zenith",
  "The request failed.": "Falló la petición.",
  "Copied with sources": "Copiado con las fuentes",
  "Copy answer": "Copiar respuesta",
  "Searching your documents": "Buscando en tus documentos",
  "Writing the answer": "Redactando la respuesta",
  "No answers yet": "Todavía sin respuestas",
  "Couldn't delete that document.": "No se pudo eliminar el documento.",
  "Couldn't load documents.": "No se pudieron cargar los documentos.",
  "No documents in this folder.": "No hay documentos en esta carpeta.",
  "No documents yet — Upload is in the nav to get started.": "Aún no hay documentos. Empieza por Subir, en el menú.",
  "Show more": "Ver más",
  "Select all": "Seleccionar todo",
  "Cancelled": "Cancelado",
  "The upload failed.": "Falló la subida.",
  "That document is no longer available.": "Ese documento ya no está disponible.",
  "The document could not be opened.": "No se pudo abrir el documento.",
  "No question matches that.": "Ninguna pregunta coincide.",
  "Show older": "Ver anteriores",
  "You have not asked anything yet.": "Todavía no has preguntado nada.",
  "Checking…": "Comprobando…",
  "Merge": "Fusionar",
  "Merge and widen access": "Fusionar y ampliar el acceso",
  "Merging…": "Fusionando…",
  "No labels match.": "Ninguna etiqueta coincide.",
  "Preview changes": "Ver los cambios",
  "This tenant has no labels yet.": "Esta organización aún no tiene etiquetas.",
  "No labels match those filters.": "Ninguna etiqueta coincide con esos filtros.",
  "Hide ranking detail": "Ocultar el detalle de la ordenación",
  "The search failed.": "Falló la búsqueda.",
  "Why these results?": "¿Por qué estos resultados?",
  "That was not created.": "No se creó.",
  "Your profile couldn't be loaded.": "No se pudo cargar tu perfil.",
  "Organisation": "Organización",
  "Member since": "Miembro desde",
  "Documents uploaded": "Documentos subidos",
  "No roles assigned": "Sin roles asignados",
  "Labels you reach": "Etiquetas que alcanzas",
  "A document is visible to you only if it carries one of these — or none at all.": "Un documento sólo es visible para ti si lleva una de éstas, o ninguna.",
  "None — you see only unlabelled documents": "Ninguna: sólo ves documentos sin etiqueta",
  "Permissions": "Permisos",
  "Set a name": "Poner un nombre",
  "That name couldn't be saved.": "No se pudo guardar el nombre.",

  // --- document panel -------------------------------------------------------------------
  Pages: "Páginas",
  Passages: "Pasajes",
  "Cited by": "Citado por",
  "{count} answer": { one: "{count} respuesta", other: "{count} respuestas" },
  Uploaded: "Subido",
  "someone since removed": "alguien que ya no está",
  Size: "Tamaño",
  "Characters {from}–{to}": "Caracteres {from}–{to}",

  // --- access and roles -----------------------------------------------------------------
  "Search labels to map": "Buscar etiquetas que asignar",
  "Clearance required by {label}": "Habilitación que exige {label}",
  "Applies to this label everywhere, not just to this group — saved as soon as you change it.":
    "Se aplica a esta etiqueta en todas partes, no sólo en este grupo; se guarda en cuanto lo cambias.",
  "New group name": "Nombre del nuevo grupo",
  "Delete {group}? Its {count} member(s) lose whatever it opened.": {
    one: "¿Eliminar {group}? Su único miembro perderá lo que le abría.",
    other: "¿Eliminar {group}? Sus {count} miembros perderán lo que les abría.",
  },
  "New role name": "Nombre del nuevo rol",
  "Clearance of {role}": "Habilitación de {role}",
  "needs clearance {level}": "requiere habilitación {level}",
  "Delete label {label}": "Eliminar la etiqueta {label}",
  "No labels match “{term}”.": "Ninguna etiqueta coincide con «{term}».",
  "Filter by {name}": "Filtrar por {name}",

  // --- analytics ------------------------------------------------------------------------
  Questions: "Preguntas",
  "People asking": "Personas que preguntan",
  "Average answer": "Respuesta media",
  "{ms} ms retrieval": "{ms} ms de recuperación",
  Tokens: "Tokens",
  "{n} prompt": "{n} de entrada",
  "{n} completion": "{n} de salida",
  "{count} question reported no usage — not counted": {
    one: "{count} pregunta no informó de consumo; no se cuenta",
    other: "{count} preguntas no informaron de consumo; no se cuentan",
  },
  "Nobody has asked anything yet.": "Nadie ha preguntado nada todavía.",
  "a deleted user": "un usuario eliminado",
  "No answer has cited anything yet.": "Ninguna respuesta ha citado nada todavía.",

  // --- the rest -------------------------------------------------------------------------
  "Switch to {theme}": "Cambiar a {theme}",
  "Send this link to {email}.": "Envía este enlace a {email}.",
  "Repeat it": "Repítela",
  "Repeat the password": "Repite la contraseña",
  "Search questions": "Buscar preguntas",
  "Ranking passages": "Ordenando pasajes",
  "Nothing matches that.": "Nada coincide.",
  "{count} citation": { one: "{count} cita", other: "{count} citas" },
  "{count} documents": { one: "{count} documento", other: "{count} documentos" },
  "you": "tú",
  "a colleague": "otra persona",
  "Last {days} days": "Últimos {days} días",
  "{percent}% of questions": "{percent}% de las preguntas",
  "active": "activa",
  "suspended": "suspendida",
  "purging": "purgando",
  "purged": "purgada",
  Active: "Activas",
  Suspend: "Suspender",
  Activate: "Activar",
  Purge: "Purgar",
  Create: "Crear",
  "Destroyed organisations ({count})": "Organizaciones destruidas ({count})",
  "This link is no longer valid. Ask your administrator for a new one.":
    "Este enlace ya no es válido. Pide otro a tu administrador.",
  "Ingesting {count} documents": { one: "Procesando {count} documento", other: "Procesando {count} documentos" },
  "Ingesting {count} of {total}": { one: "Procesando {count} de {total}", other: "Procesando {count} de {total}" },
  "{count} failed to process": { one: "{count} ha fallado", other: "{count} han fallado" },
  "Sign out on this device. Your other sessions are untouched.":
    "Cierra la sesión en este dispositivo. Las demás no se tocan.",
  "Sign out":
    "Cerrar sesión",
  "Purge permanently":
    "Purgar permanentemente",

  // --- the anchored conversation (F25) --------------------------------------------------
  "Document preview": "Vista del documento",
  "Ask about this document": "Preguntar sobre este documento",
  "Back to the results": "Volver a los resultados",
  "{count} passage": { one: "{count} pasaje", other: "{count} pasajes" },
  "Nothing matches this closely": "Nada coincide con claridad",
  "What follows is the nearest thing in your documents, not an answer.":
    "Lo que sigue es lo más parecido que hay en tus documentos, no una respuesta.",
  "Closest passages": "Lo más cercano",
  "Nothing in your documents is about “{query}”.":
    "Ninguno de tus documentos trata sobre «{query}».",
  "This is not a wording problem — rephrasing will not help. The corpus does not cover this subject.":
    "No es cuestión de cómo lo escribas: reformularlo no va a servir. El corpus no cubre este tema.",
  "Copy the highlighted passage": "Copiar el pasaje resaltado",
  Copied: "Copiado",
  "Open a document from a search to ask about it":
    "Abre un documento desde una búsqueda para poder preguntar sobre él",
  "Answering from {filename} only.": "Respondiendo sólo desde {filename}.",
};
