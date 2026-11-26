# Memoria della validazione MeteoHub

| Struttura | Crescita con i dati | Gestione |
|---|---|---|
| Osservazioni durante la lettura | Una voce per istante distinto e una voce per intervallo di pioggia, raggruppato per ora. Anche i duplicati occupavano una voce nella lista. | Conserva un solo valore o un marcatore di ambiguità per istante. Libera la mappa di lettura prima della ricostruzione oraria della pioggia. |
| `observations` | Una voce per osservazione accettata con chiave rete, stazione, istante e variabile. | Nel validatore contiene solo le misure orarie. Gli altri chiamanti del caricatore continuano a ricevere tutte le misure accettate. |
| `nearby_observations` | Una coppia scarto/valore per osservazione non oraria accettata e un elenco per ora, stazione e variabile. | Riceve le misure non orarie direttamente dal caricatore. Mantiene l'ordine per vicinanza e poi per istante, senza una seconda voce in `observations`. Si libera dopo l'abbinamento. L'indice resta in RAM per evitare accessi al disco per ogni ricerca. |
| Partizioni temporanee | Un record su disco per coppia abbinata. Prima la RAM necessaria per un blocco cresceva con il blocco più grande. | Mantiene 32 blocchi e ne registra le dimensioni. Oltre 64 MiB di dati serializzati, il blocco viene ordinato in SQLite con cache configurata a 8 MiB e letto un evento alla volta. La soglia riguarda il file serializzato, la rappresentazione Python in RAM è più grande. |
| `official_errors`, `exact_time_errors`, `preliminary_errors` | Prima un numero a 64 bit per errore in ogni campione di appartenenza. | Una conta e tre somme a precisione compensata per modello, variabile e fascia di anticipo. La memoria di queste strutture dipende soltanto dal numero di combinazioni. |

L'indice mantiene la precedenza dell'istante esatto, l'assegnazione
della mezz'ora all'ora seguente e la scelta della misura precedente
in caso di parità. Ogni confronto raggruppa stazione, ora, ciclo e
variabile. I duplicati restano esclusi anche quando arrivano da file
distinti. Per la pioggia continua a servire un accumulo orario completo.

MAE, RMSE e bias mantengono formule e arrotondamento a quattro decimali.
La somma compensata riduce l'errore di arrotondamento in virgola mobile.
Un valore a cavallo di una soglia di arrotondamento può differire
nell'ultima cifra pubblicata rispetto alla somma precedente. Conteggi,
campioni, esiti di pioggia e diagnostica degli abbinamenti si confrontano
per uguaglianza esatta.

## Riprodurre i controlli

Eseguire dalla radice del repository con Python 3.11 o successivo.
Il test usa dati sintetici, prova sia i blocchi in RAM sia il percorso
SQLite e verifica tutti e sette gli output contro la versione precedente
quando è fornita tramite la variabile d'ambiente.

```bash
git show ddf0d0d9bb308ff62a68666dea0d51d9ef344023:validate_meteohub.py > /tmp/meteohub-before.py
METEOHUB_VALIDATOR_BASELINE=/tmp/meteohub-before.py python -m unittest discover -s tests -p test_meteohub_validator.py -v
```

Il benchmark Linux riporta righe e file effettivamente elaborati,
tempo della validazione, picco RSS del processo e spazio temporaneo
campionato ogni 50 ms. I log dei blocchi riportano anche le dimensioni
esatte dopo la scrittura. Eseguire ogni versione in un processo nuovo
e mantenere identici gli input e l'orario di riferimento. Le directory
di output devono essere nuove.

```bash
python tests/benchmark_meteohub_validator.py --validator /tmp/meteohub-before.py --output /tmp/meteohub-bench-before
python tests/benchmark_meteohub_validator.py --output /tmp/meteohub-bench-after
```

I due comandi usano per impostazione predefinita il campione sintetico
(18 osservazioni e 90 previsioni su due file). Per un confronto su scala
maggiore, passare `--observations <CSV>`, `--forecasts <directory>`
e `--now <data ISO 8601 con fuso>` a entrambi. Se sono disponibili gli
archivi delle decisioni prospettiche, specificare anche `--historical-dir`
con la directory che contiene `historical/forecasts`.
