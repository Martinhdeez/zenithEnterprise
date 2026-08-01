# Decisiones Técnicas — Zenith Enterprise

**Fecha:** 2026-07-27
**Estado:** Candidatos aprobados. Confirmación definitiva sujeta a las mediciones del hito M0.
**Documentos relacionados:** `2026-07-27-plan-de-iteraciones.md`, `2026-07-27-mvp.md`

> **Naturaleza de este documento.** Los modelos aquí elegidos son **candidatos**, no decisiones cerradas. El ranking de modelos de embeddings se mueve cada pocos meses. Lo que no caduca es el método: validar contra el conjunto de evaluación propio antes de fijar. Y gracias al pipeline de reindexado (§4), equivocarse cuesta horas de GPU en lugar de una reescritura.

---

## Resumen del stack

| Componente | Elección | Licencia |
|---|---|---|
| Embeddings | BGE-M3 | MIT |
| Re-ranker | bge-reranker-v2-m3 | MIT |
| Almacenamiento, búsqueda y colas | Postgres + pgvector + pg_search | Open source |
| Fusión híbrida | Reciprocal Rank Fusion (k=60) | — |
| Cola asíncrona | procrastinate | Open source |
| Parseo PDF | pdfplumber (rápido) + Docling (pesado) | MIT / MIT |
| Transcripción *(iteración 2)* | faster-whisper + WhisperX | MIT |
| Diarización *(iteración 2)* | pyannote.audio | Uso comercial permitido |
| Servido de embeddings | Text Embeddings Inference (TEI) | Apache 2.0 |
| LLM enchufable | Interfaz compatible con OpenAI | — |

---

## 1. Embeddings: BGE-M3

**`BAAI/bge-m3`.** Licencia MIT, uso comercial sin restricciones.

Tres razones, en orden de peso:

**Genera representación densa y dispersa con un solo modelo.** Cubre la búsqueda semántica y la léxica exacta desde un único modelo. También produce vectores multi-representación estilo ColBERT si se necesitan.

**Multilingüe de serie (100+ idiomas).** Se arranca en inglés en el MVP y, al abrir a español, no hay que cambiar de modelo. Aunque el reindexado (§4) hace ese cambio asumible, evitarlo sigue siendo preferible.

**Contexto de 8192 tokens**, que da libertad en el tamaño de fragmento.

**Candidatos contra los que medir en M0:**

| Modelo | Licencia | Nota |
|---|---|---|
| `intfloat/multilingual-e5-large` | MIT | Sólido y más ligero. Requiere prefijos `query:` / `passage:`; es asimétrico y se implementa mal con facilidad |
| `Alibaba-NLP/gte-multilingual-base` | Apache 2.0 | Buena relación calidad/tamaño |
| `nomic-ai/nomic-embed-text-v2` | Apache 2.0 | Embeddings Matryoshka: permite truncar dimensiones para ahorrar almacenamiento |

**Descartado:** Jina embeddings v3, pese a su calidad. Licencia CC-BY-NC, no comercial. Para una startup es un bloqueo, no un detalle.

---

## 2. Re-ranker: bge-reranker-v2-m3

**`BAAI/bge-reranker-v2-m3`.** MIT, multilingüe, misma familia que el embedder.

Es un cross-encoder: procesa pregunta y fragmento juntos en lugar de comparar vectores. Mucho más preciso y mucho más caro, por eso solo se aplica a los finalistas.

**Configuración:** recuperar 50-100 candidatos, reordenar, quedarse con los 8 mejores para el LLM.

**Alternativa si hace falta más potencia:** `mxbai-rerank-v2` (Apache 2.0).

---

## 3. Almacenamiento, búsqueda y colas: Postgres

**Postgres con `pgvector` para vectores, `ParadeDB/pg_search` (o FTS nativo) para búsqueda léxica, y `procrastinate` para colas.**

Es deliberadamente la opción aburrida. Razones, en orden de peso:

**Vendemos on-premise.** Cada servicio adicional es una carga operativa que se traslada al cliente. Desplegar un Postgres en la infraestructura de una empresa es trivial. Pedirle al equipo de IT que además mantenga un clúster de Redis y vigile su persistencia es un punto de fricción que acaba pagándose en soporte.

**RF-04 vive naturalmente ahí.** Tenants, roles y ACLs son datos relacionales. Tenerlos en la misma base transaccional que los vectores permite aplicar el aislamiento con Row-Level Security (§5), no con disciplina de programación.

**A nuestra escala llega de sobra.** 100.000 documentos son del orden de 1-2 millones de fragmentos. pgvector con índice HNSW lo soporta sin problema.

**Idempotencia gratis (RNF-05).** Si la transacción falla, el estado de la cola se revierte junto con la inserción de los fragmentos. Cero inconsistencias, sin código adicional.

**Deduplicación gratis (RF-03.3).** Restricción de unicidad sobre el hash del archivo, en la misma base.

**Borrado en cascada gratis.** Eliminar un documento elimina sus páginas, fragmentos y vectores en la misma transacción — el mínimo exigible para GDPR. Repartido entre dos sistemas sería una fuente permanente de datos huérfanos.

**Separación de pools de conexión.** Como Postgres concentra todas las cargas, las lecturas de usuario (búsqueda) usan un pool distinto del de los workers de ingesta y reindexado. Impide que una ráfaga de escrituras agote las conexiones disponibles para consultar. Detalle y mitigaciones adicionales en `mvp.md` §3.7.

*Precisión frecuente: `procrastinate` no sondea la cola en bucle — usa `LISTEN`/`NOTIFY`, así que los workers esperan notificación. La presión de I/O por ese lado es menor de lo que suele suponerse; la contención real está en CPU de búsqueda HNSW y en escrituras masivas de ingesta.*

**Cuándo migrar a Qdrant — criterio medido, no intuición.** El filtrado por etiquetas de acceso (RF-04.2) se aplica **dentro** de la consulta vectorial, y el pre-filtrado agresivo degrada el índice HNSW: si un usuario alcanza el 5% del corpus, el índice recorre mucho más para reunir 50 candidatos.

M0 mide Recall@50 y p95 con el usuario alcanzando el 100%, el 20% y el 5% del corpus. **Si la caída supera el 5%, la migración a Qdrant queda justificada con datos** — en la semana cuatro y con la base vacía, no con clientes en producción.

---

## 4. Reindexado en caliente y espacios vectoriales

Satisface RNF-08. Es una de las tres decisiones que son arquitectura y no higiene.

### 4.1 El problema

Un embedding solo tiene sentido dentro del modelo que lo generó. Los vectores de dos modelos distintos **no son comparables entre sí**: es medir en centímetros y en pulgadas sin factor de conversión. Por tanto no puede haber medio corpus embebido con un modelo y medio con otro — las búsquedas devolverían resultados sin sentido.

Sin previsión, cambiar de modelo obliga a: parar el sistema, borrar todos los vectores, regenerarlos (decenas de horas de GPU sobre un corpus real) y confiar en que el modelo nuevo sea efectivamente mejor, porque los vectores antiguos ya no existen.

**Con clientes en producción eso es inasumible.** Y su efecto de fondo es peor: paraliza, porque obliga a acertar a la primera.

### 4.2 La solución

**Que quepa más de un espacio vectorial a la vez.** Los embeddings no viven en una columna de `chunks`, sino en tabla propia:

```
chunk_embeddings
  chunk_id
  embedding_model      -- "bge-m3"
  embedding_version    -- "1.0"
  embedding            -- vector
  PRIMARY KEY (chunk_id, embedding_model, embedding_version)

embedding_spaces
  model, version, dimension, status   -- activo | construyendo | retirado
```

Las búsquedas consultan siempre el espacio marcado como `activo`. Cambiar de modelo es cambiar una fila.

*Detalle: si el modelo nuevo tiene otra dimensión, pgvector exige columna nueva, así que la migración crea una tabla por espacio en vez de filas adicionales. El concepto no cambia.*

### 4.3 Reindexar no es reprocesar

El esquema persiste cada etapa por separado: `pages.text` guarda el resultado del parseo y `chunks.text` el del troceado.

Volver a parsear 60.000 PDFs con Docling son días. Volver a embeber texto ya extraído son horas. **Separar y persistir las etapas permite rehacer solo la última.** Es la razón de fondo por la que el modelo de datos tiene esa forma.

### 4.4 Procedimiento de conmutación

1. Se registra el modelo nuevo como espacio en estado `construyendo`.
2. Un trabajo en segundo plano genera los vectores nuevos **con el sistema sirviendo con normalidad** sobre el espacio activo. Tarda lo que tarde.
3. Al terminar, se ejecuta el arnés de evaluación contra el espacio nuevo y se compara con el Recall real del activo — no con el de las pruebas de laboratorio.
4. Si mejora, se marca `activo`. El cambio es instantáneo.
5. El anterior se mantiene una semana en `retirado` por si hay que revertir.

**Cero minutos de caída. Reversión en un segundo.**

### 4.5 Versionado de la estrategia de segmentación

El mismo razonamiento aplica al troceado. `chunks.chunking_version` permite desplegar una estrategia nueva progresivamente y comparar cuál recupera mejor con datos reales.

---

## 5. Aislamiento y ámbito: Row-Level Security

**Ambos niveles de filtrado se aplican con RLS de Postgres, no con cláusulas `WHERE` en el código de aplicación.**

| Nivel | Requisito | Política |
|---|---|---|
| Entre tenants | RF-04.1 | `app.tenant_id` de la sesión |
| Entre roles del mismo tenant | RF-04.2 | Etiquetas de acceso alcanzables por el rol |

Una fuga de datos entre clientes es el peor escenario posible del producto. Con filtrado en aplicación, basta con que una consulta olvide un `WHERE` para provocarla. Con RLS, la base de datos rechaza las filas aunque el código lo olvide. **Deja de ser una cuestión de disciplina y pasa a ser una garantía estructural.**

Implementación: la sesión establece `SET LOCAL app.tenant_id` y las etiquetas alcanzables; cada tabla lleva su política.

**Pruebas obligatorias.** Dos tests automatizados, los más importantes del producto:

1. **Fuga entre tenants** — dos tenants con documentos, verificando que ninguno alcanza los del otro por búsqueda vectorial, BM25, listado ni acceso directo por ID.
2. **Fuga entre etiquetas** — dentro de un mismo tenant, un rol sin acceso a una etiqueta no obtiene respuestas basadas en esos documentos **ni puede inferir que existen**: ni en citas, ni en listados, ni en mensajes del tipo "hay algo que no puedes ver".

Son la respuesta que se enseña cuando un cliente corporativo pregunte por el aislamiento.

---

## 6. Búsqueda híbrida: fusión RRF

```
Pregunta del usuario
   │
   ├──► BM25 / pg_search ────► top 50   (exacto: códigos, nombres, siglas)
   │
   └──► Vector denso ────────► top 50   (semántico: intención, sinónimos)
                │
                ▼
        Reciprocal Rank Fusion  ──────► top 50 combinado
                │
                ▼
        Cross-encoder re-ranker ──────► top 8
                │
                ▼
        LLM enchufable + citas
```

**Fórmula:**

```
score(d) = Σ  1 / (k + rank_i(d))        con k = 60
```

**Por qué la parte léxica es imprescindible.** La búsqueda semántica pura falla justo con identificadores exactos: códigos de producto, números de contrato, nombres propios, siglas internas. Si un empleado busca la factura `FAC-2026-99`, el vector busca el *concepto* de factura, no la cadena exacta. En un corpus corporativo eso es una fracción enorme de las consultas reales.

**Por qué RRF y no normalizar puntuaciones.** Las puntuaciones de BM25 y la similitud coseno no son comparables: viven en escalas distintas y dependientes del corpus. Normalizarlas es frágil y exige recalibrar con cada cambio de datos. RRF usa **solo las posiciones**, es robusto y prácticamente no tiene parámetros que ajustar.

---

## 7. Parseo de PDF: enrutado por página

La calidad de la extracción determina el techo de todo lo demás. Si el parser destroza una tabla, ningún embedder la recupera bien. Las tablas importan especialmente aquí: contratos, presupuestos y documentos financieros son tabla pura, y es donde están las cifras que la gente pregunta.

**Problema:** Docling entiende la maquetación mediante modelos de visión y es computacionalmente pesado. Un manual técnico de 1.500 páginas tardaría horas.

**Solución: enrutar por página, no por documento.** Ese manual probablemente tiene 40 páginas con tablas y 1.460 de texto corrido. Por documento se paga Docling sobre las 1.500. Por página, se procesan 1.460 en segundos y se reserva Docling para las 40 que lo necesitan.

**Criterio de decisión, medible:**

```
¿La página tiene capa de texto extraíble?
   No  → escaneada          → Docling con OCR
   Sí  → ¿detecta tablas?
           Sí → Docling
           No → pdfplumber (rápido)
```

`pdfplumber` (MIT) determina ambas cosas de forma barata antes de decidir.

### Extracción de coordenadas — obligatoria

Ambos parseadores deben devolver, además del texto, las **cajas delimitadoras** de cada fragmento. Son lo que permite resaltar la cita en el visor de PDF de forma fiable; el enfoque alternativo, casar índices de carácter contra la capa de texto de pdf.js, es frágil y produce resaltados desplazados (ver `mvp.md` §2.9).

Se almacenan como array de cajas —un fragmento ocupa varias líneas y puede cruzar páginas— con **coordenadas normalizadas al tamaño de página**, para que sobrevivan al zoom y al escalado del visor.

Es un dato que se obtiene durante el parseo: omitirlo obliga a **reparsear** todo el corpus, no solo a re-embeber.

### Alerta de licencia

**`PyMuPDF` es AGPL.** Es la opción más rápida y la que usan la mayoría de tutoriales, pero en un producto propietario obliga a liberar el código o a comprar licencia comercial a Artifex. **No se usa en este proyecto**, y la CI lo verifica automáticamente (§12).

---

## 8. Audio *(iteración 2, fuera del MVP)*

- **`faster-whisper`** (backend CTranslate2) con `large-v3`: unas 4 veces más rápido que la implementación original y con menos VRAM.
- **`WhisperX`** por encima, que aporta lo realmente necesario: **timestamps a nivel de palabra**. Sin ellos no se puede cumplir RF-03.2 para audio, porque los timestamps por segmento se desvían varios segundos.
- **`pyannote.audio`** para diarización — **aprobada como requisito**, no opcional. En actas de reunión corporativas, "quién dijo qué" es justamente lo que se pregunta. Requiere aceptar condiciones en HuggingFace, pero es utilizable comercialmente.

---

## 9. El conector: interfaz compatible con OpenAI

**El conector habla el protocolo de chat completions de OpenAI.** Es el estándar de facto: lo hablan de forma nativa Ollama, vLLM, llama.cpp, Azure OpenAI, Together y Groq. Bedrock y otros requieren un adaptador fino. Con una sola interfaz se cubre casi todo el mercado sin tocar código.

**Servido de modelos locales en el cliente:** vLLM si dispone de GPU adecuada, Ollama si prima la facilidad de despliegue.

**Modelo de línea base para desarrollo y CI: Llama 3.1 8B Instruct sobre Ollama.** El conector es enchufable, pero el desarrollo no puede serlo: los prompts de citación se ajustan contra un modelo fijo o ninguna regresión es reproducible. Se elige un modelo pequeño a propósito — es el suelo de lo que un cliente enchufará, y lo que se sostiene ahí se sostiene en cualquier modelo mayor. Ajustar los prompts contra un modelo cloud grande produce instrucciones que el 8B incumple, y el fallo aparece en el primer despliegue local. Alternativa Apache 2.0: Mistral 7B Instruct v0.3.

**Certificación obligatoria (RNF-06):** ningún modelo se considera soportado hasta superar una batería que mide fidelidad a las fuentes, exactitud de las citas y tasa de abstención. Sin esto, el día que un cliente enchufe un modelo pequeño y las citas fallen, el problema será nuestro.

---

## 10. Chunking y enriquecimiento contextual

- Fragmentos de ~500-800 tokens, solapamiento ~15%.
- Cortes respetando la estructura (secciones y encabezados del markdown producido por Docling), nunca tamaño fijo ciego.
- **Enriquecimiento contextual:** antes de generar el embedding, anteponer a cada fragmento 1-2 frases que sitúen de qué documento y sección procede. Se generan una vez por documento con un LLM local barato. Sube la recuperación de forma notable, porque un fragmento aislado pierde el contexto de dónde vivía.
- **Metadatos obligatorios desde el día 1:** `doc_id`, `page_num`, `bboxes`, `char_start`, `char_end`, `section`, `tenant_id`, `label_ids`, `chunking_version`. Añadirlos después obliga a reprocesar todo el corpus.

---

## 11. Gestión de VRAM y concurrencia

Hardware de referencia: **una GPU de 24 GB** (RTX 4090 o A10). Entre 1.500 y 2.000 €, o una instancia cloud modesta. Es el número que hace creíble el argumento comercial: coste marginal cero por documento, con una inversión inicial que cualquier PYME asume.

Consumo residente:

| Modelo | VRAM aprox. |
|---|---|
| BGE-M3 (fp16) | ~2,2 GB |
| bge-reranker-v2-m3 (fp16) | ~2,2 GB |
| faster-whisper large-v3 (int8) *(iteración 2)* | ~1,5 GB |
| **Total residente** | **~6 GB** |

**El riesgo de OOM no viene del tamaño de los modelos, sino de la concurrencia sin límite.** Veinte trabajos simultáneos, o lotes de embeddings sin tope, saturan los 24 GB. La solución no es más GPU:

- **Semáforo de un trabajo pesado a la vez.**
- **TEI con tamaño de lote fijo.**
- **Prioridad de colas:** las consultas son tiempo real y prioritarias; la ingesta y el reindexado ceden GPU.

Con estas cotas el consumo se mantiene plano y predecible.

---

## 12. Estándares de ingeniería

Lo que separa este producto del prototipo de hackathon. No es opinable: son requisitos de venta B2B on-premise, porque cuando algo falla en casa del cliente no hay forma de conectarse a mirar.

### 12.1 Tipado extremo a extremo

- `pyright` en modo estricto en backend. Pydantic en todas las fronteras.
- TypeScript estricto en frontend.
- **Cliente de API generado automáticamente desde el OpenAPI de FastAPI.** Backend y frontend dejan de poder divergir en silencio. Es lo que más rendimiento da por su coste.

### 12.2 Estrategia de pruebas

Tres categorías separadas, con propósitos distintos:

| Categoría | Qué cubre | Dónde corre |
|---|---|---|
| Unitarias | Lógica pura: chunking, fusión RRF, enrutado de parseo | Cada commit |
| Integración | Contra **Postgres real vía testcontainers** | Cada commit |
| Arnés de evaluación | Recall@50, Recall@8, citas | Bajo demanda (requiere GPU) |

**Nada de SQLite para pruebas.** El sistema depende de pgvector, RLS y `tsvector`, que SQLite no tiene: probar contra SQLite sería probar otro producto.

**Pruebas obligatorias por su criticidad:**
- Fuga entre tenants (§5).
- Reintento de ingesta interrumpida sin duplicados.
- Conmutación de espacio vectorial y reversión.

### 12.3 Integración continua

Desde el primer commit: `ruff` (lint y formato), comprobación de tipos, pruebas unitarias y de integración, validación de que las migraciones aplican sobre una base poblada.

**Comprobación automática de licencias** que falle ante AGPL, GPL o cláusulas no comerciales. Convierte la preocupación por PyMuPDF y Jina en una garantía mecánica en lugar de en algo que hay que recordar.

### 12.4 Seguridad del parseo

Se procesan PDFs de terceros, que es una superficie de ataque conocida:

- Verificación de tipo por **bytes mágicos**, no por extensión.
- Límite de tamaño aplicado **antes** de cargar en memoria.
- Parseo con límites de recursos y tiempo. Un PDF malicioso que agote la memoria del worker es una denegación de servicio trivial.
- Claves de API de clientes cifradas en reposo.

### 12.5 Observabilidad

Para un producto RAG no basta con registrar la respuesta. Dentro de seis meses habrá que poder responder *"¿por qué esta consulta devolvió basura?"*, y eso exige guardar **los IDs de los fragmentos recuperados y sus puntuaciones en cada fase**: BM25, vectorial, tras RRF, tras re-ranking.

Log estructurado con `structlog` desde el primer día. Sirve simultáneamente para depurar calidad con datos reales y como germen de la auditoría de consultas (iteración 4).

### 12.6 Taxonomía de errores

Nada de `try/except` ad hoc. Catálogo cerrado de fallos de ingesta mapeado a mensajes legibles: PDF cifrado, sin capa de texto, corrupto, demasiado grande, timeout de parseo. Es lo que hace posible la promesa de "motivo legible" en la interfaz.

Separación explícita entre error de usuario (400, mensaje humano) y error de sistema (500, mensaje genérico + identificador de traza en el log).

### 12.7 Fuera de alcance por ahora

Solidez para problemas que aún no existen: SSO, alta disponibilidad, Kubernetes, microservicios, feature flags, pruebas end-to-end de interfaz.

---

## 13. Prohibiciones explícitas

| Elemento | Motivo |
|---|---|
| `PyMuPDF` | AGPL — obligaría a liberar el código o pagar licencia |
| Jina embeddings v3 | CC-BY-NC — no comercial |
| LangChain / LlamaIndex en el núcleo | Ocultan la recuperación tras capas de abstracción, y la recuperación es el producto |
| Redis u otros servicios adicionales | Cada servicio es una llamada de soporte en despliegue on-premise |
| SQLite en pruebas | No tiene pgvector, RLS ni `tsvector` |
| Filtrado por tenant solo en aplicación | Una cláusula olvidada es una fuga de datos entre clientes |
