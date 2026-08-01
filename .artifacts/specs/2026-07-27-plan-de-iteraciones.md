# Plan de Iteraciones — Zenith Enterprise

**Fecha:** 2026-07-27
**Estado:** Iteración 1 (MVP) convergida y detallada en documento propio. Iteraciones 2-4 esbozadas.
**Documentos relacionados:** `2026-07-27-mvp.md`, `2026-07-27-decisiones-tecnicas.md`

> **Nota de consolidación.** La versión inicial de este plan contemplaba una **iteración 0** separada, como spike desechable previo. Se ha eliminado: el proyecto Zenith de HackUDC 2026 ya validó la idea y el stack, y la medición de calidad de recuperación que justificaba ese spike se ha plegado dentro del MVP como su primer hito (M0), con arnés permanente en lugar de código desechable. Lo que era "iteración 0 + iteración 1" es ahora **la iteración 1: el MVP**.

---

## 1. Marco de trabajo

### 1.1 Por qué no un ciclo en V monolítico

El ciclo en V clásico asume que los requisitos se conocen por completo antes de diseñar, y que la validación ocurre al final. Zenith Enterprise no cumple esa premisa, porque sus riesgos dominantes no son de especificación sino **empíricos**: la calidad de recuperación semántica sobre documentos corporativos reales no se puede predecir sobre el papel, y la precisión de las citas depende de la estrategia de segmentación, que solo se valida midiendo.

### 1.2 Marco adoptado: V incremental

Se conserva el rigor del ciclo en V, aplicado **por iteración** en lugar de al producto completo. Cada iteración recorre su propia V, delgada:

```
Requisitos de la iteración  <-->  Criterios de aceptación
        │                                  ▲
        ▼                                  │
   Diseño                    <-->     Pruebas de integración
        │                                  ▲
        └──────────► [ CÓDIGO ] ───────────┘
```

Consecuencias prácticas:

- El **SRS de sistema** se redacta completo. Es el contrato estable del proyecto.
- El **diseño detallado** se hace por iteración. No se diseña en detalle lo que no se toca hasta la iteración 3.
- Cada iteración declara **qué IDs de requisito cierra**. Esa trazabilidad aporta la mayor parte del valor del ciclo en V a una fracción del coste.

**Coste asumido:** se renuncia a poder afirmar que el sistema está totalmente especificado antes de empezar. Si aparece un cliente que exige documentación V estricta (defensa, sanidad), habrá que reconstruir esa narrativa a partir de la trazabilidad por IDs. Es viable, pero es trabajo adicional.

### 1.3 Principio de ordenación

> Cada iteración retira el mayor riesgo restante, no entrega las funcionalidades más vistosas.

### 1.4 Gestión operativa

- Un artifact por iteración, moviéndose por el pipeline: `todo/` → `in-progress/` → `to-test/` → `tested/` → `shipped/`.
- Este documento vive en `specs/` y no se mueve por el pipeline.

### 1.5 Cómo se formulan los criterios de aceptación

Un criterio de aceptación necesita cinco elementos. Si le falta alguno, no es un criterio: es un deseo.

1. **Qué se mide** — la métrica exacta.
2. **Sobre qué** — qué conjunto de datos y cuántos casos.
3. **Qué umbral** — el número.
4. **Cómo se mide** — procedimiento reproducible, mismo resultado si lo ejecuta otra persona.
5. **Qué pasa si falla** — la consecuencia.

#### Gates y mediciones

Distinción central del proyecto. Poner un umbral a algo que nunca se ha medido es rigor falso: o se cumple por casualidad, o se acaba bajando el umbral para que encaje.

- **Gate** — umbral duro, justificable *antes* de medir. Si no se cumple, la iteración no se cierra. Deben ser pocos.
- **Medición** — se registra el número sin umbral. Se convierte en la **línea base** contra la que se juzga la iteración siguiente.

**El hito M0 del MVP tiene pocos gates y muchas mediciones, y es correcto que sea así**: su propósito es precisamente generar las líneas base que permiten fijar umbrales legítimos después.

#### De dónde sale un número legítimo

Solo hay tres fuentes válidas:

1. **Consecuencia de negocio** — una cita errónea destruye la confianza del cliente, luego el umbral es alto. Una respuesta lenta molesta pero se tolera, luego es más flojo.
2. **Restricción arquitectónica** — si se envían 8 fragmentos al LLM, lo que no esté entre esos 8 es irrecuperable. El umbral se deriva de la estructura del pipeline.
3. **Línea base medida** — se mide, y el umbral siguiente es "no empeorar".

Si un número no se puede justificar por una de las tres, se escribe **"medir en M0, fijar umbral después"**. Es preferible a inventarlo.

---

## 2. Arquitectura de IA: qué es fijo y qué es enchufable

Decisión estructural que condiciona todo el plan.

### 2.1 IA local fija — coste marginal cero y privacidad total

Todo el procesamiento e indexación corre siempre en local. Son tareas mecánicas, sin necesidad de razonamiento:

- **Generación de embeddings** — los documentos nunca salen del servidor.
- **Transcripción de audio** — Whisper en local.
- **Re-ranking** — el filtro de precisión sobre los resultados de búsqueda.

### 2.2 Conector libre (model-agnostic) — solo la redacción final

Únicamente la generación de la respuesta final es intercambiable. El cliente enchufa lo que su política permita: un modelo open source local, o una API cloud empresarial.

### 2.3 Consecuencias

**Sobre el coste (RNF-02):** el coste variable escala con **consultas**, no con volumen documental. Indexar 100.000 documentos cuesta lo mismo en API que indexar 100: cero. Esto hace RNF-02 sostenible.

**Sobre el riesgo de calidad:** en RAG, la calidad de la respuesta la domina la **recuperación**, no la generación. Si el retriever entrega los fragmentos equivocados, ningún modelo lo corrige — produce una respuesta fluida, segura y falsa. Los componentes fijados a local (embeddings + re-ranking) son por tanto los de mayor peso en la corrección **y los que no tienen válvula de escape**.

**Sobre la reversibilidad del modelo de embeddings.** Cambiar de modelo de embeddings invalida todos los vectores existentes: los espacios vectoriales de dos modelos distintos no son comparables entre sí. Sin previsión, eso convierte la elección del embedder en una puerta de un solo sentido.

> **Mitigación adoptada.** Se construye desde el primer día un **pipeline de reindexado en caliente** (ver decisiones técnicas §4): el esquema admite varios espacios vectoriales conviviendo, se genera el nuevo en segundo plano mientras el antiguo sigue sirviendo consultas, y la conmutación es un cambio de estado reversible.
>
> El efecto es estratégico: **deja de ser obligatorio acertar con el modelo a la primera.** Cambiar de embedder pasa a costar horas de GPU en lugar de una reescritura y una ventana de caída. Esto también aplica a la estrategia de segmentación.

**Sobre el conector:** que sea enchufable no basta. La fidelidad de las citas (RF-03.2) depende de que el modelo respete las instrucciones de atribución, y eso varía enormemente entre modelos. Se requiere una **batería de certificación** que todo modelo deba superar antes de considerarse soportado (RNF-06).

---

## 3. Decisiones asumidas

| Decisión | Valor |
|---|---|
| Modelo de despliegue | Híbrido (SaaS + opción on-premise) |
| Estrategia híbrida | Arquitectura preparada para ambos desde el día 1, pero **un solo modo desplegado** hasta la iteración 3 |
| Entorno de desarrollo y demo | **VPS propia**, Docker Compose. Es el entorno donde se construye y se enseña el producto |
| Despliegue en design partner | **TBD — pendiente de conversación comercial.** No se fija hasta que haya un sí |
| Design partners | **TBD.** Candidatos tanteados, ninguno confirmado |
| Idioma del MVP | **Inglés** |
| Modelo de embeddings | Multilingüe pese al arranque en inglés, para no reindexar al abrir a español |
| Reversibilidad del embedder | Pipeline de reindexado en caliente desde el día 1 |
| Punto de partida de código | **Repositorio nuevo.** El proyecto de hackathon se consulta como referencia, no se hereda (ver `mvp.md` §1.3) |
| Corpus de evaluación | Mixto: ~40 documentos corporativos públicos reales + ~10 sintéticos con casos adversos |
| Compromiso de latencia | Solo sobre la fase controlada por nosotros (recuperación). La generación depende del modelo enchufado por el cliente (ver §8) |
| Alcance del SRS | Completo (RF-01 a RF-04, RNF-01 a RNF-07) |
| Stack técnico | Ver `2026-07-27-decisiones-tecnicas.md` |

---

## 4. Requisitos incorporados durante la convergencia

**RNF-05 — Idempotencia y resiliencia de la ingesta.**
Todo trabajo de ingesta debe ser reintentable sin efectos duplicados. Si el procesamiento de un archivo grande falla parcialmente, el reintento no debe generar fragmentos duplicados ni estado inconsistente.
*Se satisface con la cola respaldada por Postgres: el estado de la cola se revierte en la misma transacción que la inserción de fragmentos.*

**RNF-06 — Certificación de modelos enchufables.**
Ningún modelo de generación se considera soportado hasta superar una batería que mide fidelidad a las fuentes, exactitud de las citas y tasa de abstención ante preguntas sin respuesta en el corpus.

**RNF-07 — Gobernanza de datos en testing.**
Queda estrictamente prohibido el uso de APIs cloud de terceros para generar datasets de evaluación o pruebas a partir de documentos reales de clientes. Todo testing con documentos de cliente se realiza exclusivamente con el stack local.
*Los modelos de frontera cloud sí se pueden usar sobre el corpus de desarrollo propio o público.*

**RNF-08 — Reversibilidad del espacio vectorial.**
El sistema debe permitir generar un espacio vectorial nuevo en segundo plano, evaluarlo contra el arnés y conmutar a él sin interrupción del servicio, manteniendo el anterior disponible para revertir.

**Pendientes de decisión:** ciclo de vida del dato más allá del borrado en cascada, auditoría de consultas como funcionalidad expuesta, feedback sobre respuestas. Ver §9.

---

## 5. Iteraciones

### Iteración 1 — MVP

**Duración estimada:** ~5-6 meses
**Requisitos que cierra:** RF-01.1, RF-03.1, RF-03.2, RF-03.3, RF-04.1, **RF-04.2**, RNF-04, RNF-05, RNF-06, RNF-07, RNF-08

**Especificación completa en `2026-07-27-mvp.md`.** Resumen:

Producto instalable en 2-3 design partners, no vendible por web. Documentos PDF en inglés, consulta en lenguaje natural, respuesta con cita verificable a nivel de página, multi-tenancy con aislamiento garantizado por Row-Level Security, RBAC con roles configurables y ámbito documental por etiquetas, conector LLM configurable con certificación.

> **RF-04.2 se adelanta desde la iteración 4.** Un design partner sube su corpus real desde el primer día, y contiene nóminas, actas de dirección y expedientes. Un sistema donde todos los empleados lo ven todo no supera ni el arranque del piloto. La planificación original asumía un piloto con documentación acotada, y no es realista. Coste: 2-3 semanas.

Seis hitos: M0 pipeline y arnés de evaluación · M1 ingesta productiva · M2 consulta y citas · M3 multi-tenancy y autenticación · M4 administración y operación · M5 empaquetado.

**Puerta crítica:** M0 termina con los cuatro gates de calidad de recuperación. Si no pasan, no se construye interfaz encima.

**Fuera de alcance:** audio, español, categorización automática, facetas, permisos por documento individual, jerarquías de carpetas, SSO, conectores externos, memoria conversacional.

---

### Iteración 2 — Audio y reuniones

**Duración estimada:** por estimar tras el MVP
**Requisitos que cierra:** RF-01.2, extensión de RF-03.2 al medio audio

**Alcance**

- Ingesta de archivos de voz y grabaciones de reuniones.
- Transcripción con **timestamps a nivel de palabra** — imprescindible, porque los timestamps por segmento se desvían varios segundos y hacen inútil la cita.
- Indexación del texto transcrito.

**Riesgos conocidos**

- Calidad de transcripción con varios interlocutores y solapamiento de voz.
- Coste computacional de la transcripción frente a RNF-02.
- Coste computacional de la diarización, que se suma al de la transcripción.

**Diarización — aprobada como requisito.** Identificar quién habla (`pyannote.audio`) deja de ser opcional: en actas de reunión corporativas, "quién dijo qué" es justamente lo que se pregunta. Se incorpora al alcance de la iteración desde ya.

**Criterios de aceptación**

- [ ] Una grabación de reunión se transcribe y se vuelve consultable.
- [ ] Una respuesta basada en audio cita el segundo exacto, verificable reproduciendo la grabación.
- [ ] Las intervenciones quedan atribuidas a interlocutores distinguibles.
- [ ] Una transcripción larga interrumpida se reanuda sin duplicar contenido.

---

### Iteración 3 — Organización y exploración

**Duración estimada:** por estimar
**Requisitos que cierra:** RF-02.1, RF-02.2, RF-02.3

Trabajo mayoritariamente convencional, y bastante más fácil una vez se conocen los datos reales de las iteraciones anteriores.

**Alcance**

- Extracción automática de metadatos.
- Agrupaciones virtuales por cliente, fecha y departamento.
- Filtrado facetado combinable.
- **Conectores de ingesta** (SharePoint, unidades de red, Drive) — la primera petición previsible de cualquier cliente real.
- **Borrado lógico con purga programada** (`deleted_at` + 30 días). El MVP asume borrado físico como riesgo consciente (mvp.md §2.7); aquí el filtro adicional se diseña junto al resto de la gestión documental en lugar de encajarse a posteriori sobre las rutas ya protegidas por RLS.

**Criterios de aceptación**

- [ ] Un archivo recién subido queda clasificado sin intervención manual.
- [ ] El usuario combina al menos tres filtros y obtiene resultados coherentes.
- [ ] La navegación se mantiene utilizable sobre un corpus de prueba de gran volumen.

---

### Iteración 4 — Permisos, escala y endurecimiento

**Duración estimada:** por estimar
**Requisitos que cierra:** RNF-03 y los requisitos de cumplimiento de §9

**Alcance**

- Validación de escala hasta el objetivo de RNF-03.
- Auditoría de consultas expuesta como funcionalidad (el log estructurado ya existe desde el MVP).
- SSO (SAML/OIDC), previsiblemente exigido por el primer cliente mediano.
- Ciclo de vida del dato más allá del borrado en cascada: retención y purga programada.

*RF-04.2 ya no está aquí: se adelantó al MVP.*

**Criterios de aceptación**

- [ ] El sistema mantiene el objetivo de latencia sobre un corpus del tamaño de RNF-03.
- [ ] Un administrador puede auditar qué consultó cada usuario y sobre qué documentos.

---

## 6. Trazabilidad de requisitos

| Requisito | Iteración | Nota |
|---|---|---|
| RF-01.1 Documentos | 1 | Solo PDF, solo inglés |
| RF-01.2 Audio | 2 | |
| RF-02.1 Auto-categorización | 3 | |
| RF-02.2 Vistas dinámicas | 3 | |
| RF-02.3 Navegación facetada | 3 | |
| RF-03.1 Búsqueda semántica | 1 | Validada en M0 |
| RF-03.2 Trazabilidad y fuentes | 1 (docs), 2 (audio) | Validada en M0 |
| RF-03.3 Deduplicación | 1 | |
| RF-04.1 Aislamiento de tenants | 1 | Garantizado por RLS, no por código de aplicación |
| RF-04.2 Permisos por rol | 1 | **Adelantado desde la 4.** RBAC configurable + etiquetas de acceso, también por RLS |
| RNF-01 Privacidad y soberanía | Transversal | Garantizada por diseño en indexación |
| RNF-02 Eficiencia económica | 1 (validación), transversal | |
| RNF-03 Escalabilidad | 4 | |
| RNF-04 Model-agnostic | 1 | |
| RNF-05 Idempotencia de ingesta | 1 | |
| RNF-06 Certificación de modelos | 1 | |
| RNF-07 Gobernanza en testing | 1 | Vigente desde el primer día |
| RNF-08 Reversibilidad vectorial | 1 | Esquema desde la primera migración |

---

## 7. Recomendación sobre el despliegue híbrido

El modo híbrido es el más caro de sostener. Por tanto:

1. La arquitectura lo permite desde el día 1, evitando acoplarse a servicios propietarios de un proveedor cloud concreto. RNF-04 y la elección de Postgres empujan en la misma dirección.
2. **Solo se despliega y mantiene un modo** hasta la iteración 3. Sostener dos rutas de despliegue en producción desde el principio multiplica el trabajo de operaciones sin aportar validación de producto.

---

## 8. Compromiso de latencia

El objetivo de latencia se parte en dos, porque **no es posible comprometerse con el tiempo de respuesta de un modelo que enchufa el cliente**.

| Fase | Quién la controla | Criterio |
|---|---|---|
| Recuperación + fusión RRF + re-ranking | **Nosotros** | **Gate: p95 < 500 ms** |
| Generación (hasta primer token) | El modelo del cliente | Referencia: p95 < 3 s con modelo de referencia |
| Extremo a extremo | Mixto | Referencia: p95 < 4 s hasta primer token |

Reglas de medición:

- **Siempre percentiles, nunca medias.** La media oculta exactamente los casos que expulsan al usuario. El p95 es el usuario que se está yendo.
- **Con el corpus completo cargado.** Medir latencia con 50 documentos indexados no dice nada sobre el comportamiento con 100.000. En M0 la latencia es medición; el gate de escala real llega en la iteración 4 con RNF-03.

Este reparto tiene además valor comercial: nos comprometemos contractualmente con la fase que controlamos y documentamos el resto como dependiente del modelo que elija el cliente.

---

## 9. Cuestiones abiertas

**Decididas** (registro):

- ~~Audio en el MVP~~ → **fuera**, entra en la iteración 2.
- ~~Diarización~~ → **aprobada** como requisito de la iteración 2.
- ~~Roles fijos o configurables~~ → **RBAC configurable con ámbito documental**, dentro del MVP.

**Abiertas.** Ninguna bloquea el arranque.

1. **Design partners y modo de despliegue en cliente** — **TBD, pendiente de conversación comercial.** No se fija en el documento hasta que haya un sí explícito. Mientras tanto, el desarrollo corre sobre **VPS propia** con Docker Compose, que es el mismo artefacto que se instalaría en cliente. El corpus de M0 usa equivalentes públicos, sin depender de ningún partner concreto.
2. **Rol de soporte** — ¿existe, y ve contenido del cliente o solo estado y logs? Recomendación: nunca contenido, y por escrito. Sin partner confirmado no es bloqueante, pero el catálogo de permisos de mvp.md §2.1 se cierra en M3.
3. **Requisitos de cumplimiento pendientes**: retención y purga programada, auditoría expuesta, feedback sobre respuestas. Necesarios antes de la iteración 4.
4. **Estimaciones de duración** de las iteraciones 2 a 4.

> **Nota sobre el corpus y RNF-07.** Si el design partner está identificado, el corpus de M0 debe **parecerse en tipo y estructura** a su documentación, usando equivalentes públicos. Emplear sus documentos reales para generar la red sintética con un modelo cloud violaría RNF-07.
