# Reporte de evaluación: matriz de confusión y métricas (200 pruebas)

**Proyecto:** Sistema de detección de somnolencia en Raspberry Pi 4
**Método de detección:** MediaPipe FaceMesh (EAR/MAR/pose) + score dinámico con 4 señales fisiológicas
**Documento de diseño:** [PLAN_TRABAJO_PRECISION.md](PLAN_TRABAJO_PRECISION.md)

---

## 1. Metodología

### 1.1 Definición del experimento

Se ejecutan **200 pruebas controladas** en el escenario real (conductor sentado frente a la cámara Arducam IMX519 en la Raspberry Pi 4, sistema corriendo en vivo). Cada prueba es un intervalo corto (~30 s) con una **condición real** conocida (verdad de terreno) y se registra la **predicción** del sistema:

- **Condición real** — `SOMNOLIENTO`: el sujeto simula un signo de somnolencia (microsueño, cierres prolongados repetidos, cabeceo o bostezo sostenido). `NORMAL`: el sujeto conduce/actúa despierto (incluye confusores: hablar, mirar espejos, lentes, poca luz).
- **Predicción** — `ALERTA`: el sistema comprometió **nivel ≥ 2 (SOMNOLENCIA)** o emergencia dentro de la ventana de la prueba. `NO_ALERTA`: no lo hizo.

La clase **positiva** es `SOMNOLIENTO` (lo que el sistema debe detectar).

### 1.2 Distribución de las 200 pruebas

10 escenarios × 20 repeticiones, balanceados 100 positivos / 100 negativos:

| # | Escenario | Condición real |
|---|---|---|
| 1 | Microsueño: ojos cerrados 2-3 s, de frente | SOMNOLIENTO |
| 2 | Microsueño con lentes | SOMNOLIENTO |
| 3 | Somnolencia gradual: cierres >1 s repetidos (PERCLOS) | SOMNOLIENTO |
| 4 | Cabeceo: caída de cabeza sostenida ≥3 s | SOMNOLIENTO |
| 5 | Bostezo sostenido ≥2 s | SOMNOLIENTO |
| 6 | Conducción normal, mirada al frente | NORMAL |
| 7 | Conducción normal con lentes | NORMAL |
| 8 | Hablar / reír / cantar | NORMAL |
| 9 | Mirar espejos y tablero (giros breves) | NORMAL |
| 10 | Poca luz / contraluz, conductor despierto | NORMAL |

Los escenarios negativos 7-10 se eligen deliberadamente como **confusores**: son las situaciones que históricamente generaban falsos positivos (lentes, movimiento de boca, giros de cabeza, iluminación adversa).

### 1.3 Procedimiento por prueba

1. Sistema corriendo (`./run.sh`) con calibración completada para el sujeto.
2. Se ejecuta la acción del escenario durante la ventana de prueba (~30 s).
3. Se registra `ALERTA` si el HUD/telemetría muestra nivel comprometido ≥ 2 o emergencia durante la ventana; `NO_ALERTA` en caso contrario.
4. Entre pruebas se espera el retorno a nivel 0 (o se reinicia sesión cada bloque).
5. El resultado se anota en el CSV (`pruebas_200.csv`).

### 1.4 Definición de la matriz de confusión

| | Predicción: ALERTA | Predicción: NO ALERTA |
|---|---|---|
| **Real: SOMNOLIENTO** | **VP** (verdadero positivo: detección correcta) | **FN** (falso negativo: somnolencia no detectada — el fallo peligroso) |
| **Real: NORMAL** | **FP** (falso positivo: falsa alarma) | **VN** (verdadero negativo: silencio correcto) |

### 1.5 Métricas

- **Accuracy** = (VP + VN) / N — proporción total de aciertos.
- **Precisión** = VP / (VP + FP) — de las alertas emitidas, cuántas eran somnolencia real.
- **Recall (sensibilidad)** = VP / (VP + FN) — de las somnolencias reales, cuántas se detectaron. *Es la métrica crítica de seguridad: un FN es un conductor dormido sin alarma.*
- **Especificidad** = VN / (VN + FP) — de las situaciones normales, cuántas se respetaron sin falsa alarma. *Es la métrica de usabilidad: un sistema que alarma en falso termina apagado.*
- **F1-score** = 2·(Precisión·Recall)/(Precisión+Recall) — balance entre ambas.

---

## 2. Ejecución

```bash
# 1. Generar la plantilla de 200 pruebas
python tools/evaluar_matriz.py --plantilla pruebas_200.csv

# 2. Ejecutar las pruebas en la Pi y completar la columna 'prediccion'
#    (ALERTA / NO_ALERTA) fila por fila

# 3. Calcular matriz + métricas y actualizar ESTE reporte automáticamente
python tools/evaluar_matriz.py pruebas_200.csv --reporte docs/REPORTE_EVALUACION_200_PRUEBAS.md
```

---

## 3. Resultados

<!-- RESULTADOS:INICIO -->
**PENDIENTE DE EJECUCIÓN.** Esta sección se completa automáticamente al correr:
`python tools/evaluar_matriz.py pruebas_200.csv --reporte docs/REPORTE_EVALUACION_200_PRUEBAS.md`
<!-- RESULTADOS:FIN -->

---

## 4. Interpretación (guía para el análisis)

Al llenar los resultados, el análisis debe cubrir:

1. **Recall**: ¿qué escenarios positivos fallaron? Un FN en microsueño (escenarios 1-2) pesa más que uno en bostezo (señal secundaria por diseño).
2. **Especificidad**: ¿qué confusores dispararon falsas alarmas? Los escenarios 7-10 validan directamente las decisiones de diseño (umbral estable con lentes, gate de pose, gate de iluminación).
3. **Comparación con el diseño**: el sistema fue rediseñado para que solo 4 señales sostenidas alerten (ver PLAN_TRABAJO_PRECISION.md §2). Los criterios de aceptación del plan son: 0 alertas nivel ≥2 en conducción normal y detección < 2 s en microsueños — que corresponden a especificidad ≈ 1.0 y recall alto en los escenarios 1-2.
4. **Limitaciones**: las pruebas son simuladas por sujetos despiertos (la somnolencia real difiere en dinámica); el tamaño por celda del desglose (20) da resolución de 5 puntos porcentuales por escenario.

## 5. Trazabilidad

Cada prueba puede respaldarse con evidencia del propio sistema:
- `SOMNO_RECORD_VIDEO=1` graba el video crudo de la sesión (`recordings/<session>.avi`).
- `SOMNO_RECORD_SESSION=1` graba el JSONL por segundo con nivel/score/EAR.
- La telemetría en Supabase (`telemetry_raw`, `events`) conserva el registro con timestamp de cada alerta emitida durante las pruebas.
