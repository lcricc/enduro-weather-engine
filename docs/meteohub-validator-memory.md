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

## Benchmark storico del 26 novembre 2026

Campione fissato alla mezzanotte UTC del 26 novembre. I due archivi permanenti sono
la [raccolta 45629364734](https://github.com/lcricc/enduro-weather-engine/releases/tag/meteohub-observations-45629364734)
e le [previsioni 45552748830](https://github.com/lcricc/enduro-weather-engine/releases/tag/meteohub-forecasts-45552748830),
compresi i segnali prospettici originali e l'ora di pubblicazione della Release.
Il benchmark verifica i due SHA-256 prima di estrarre i dati.

Prima misura locale su Linux, Python 3.12.14, due processi separati sullo stesso
host. Presenti 1.705.033 righe osservative, 2.202.928 righe previsionali e
21.000 eventi comuni, di cui 8.563 di pioggia. I sette file scientifici erano
identici anche byte per byte.

| Validatore | Elapsed | Picco RSS | Picco temporanei campionato |
|---|---:|---:|---:|
| Commit precedente `ddf0d0d` | 40,298 s | 441,41 MiB | 10.168.456 byte |
| Ottimizzazione `11bc4e6` | 38,559 s | 420,48 MiB | 10.248.000 byte |
| Variazione | −4,3% | −4,7% | +0,8% |

La verifica della procedura automatica ha ripetuto il confronto con
questi stessi archivi. Il picco RSS è sceso da 441,00 a 420,28 MiB e
il tempo da 39,387 a 37,184 s. Con SQLite forzato sui 32 blocchi, il
candidato ha impiegato 38,392 s, con 420,38 MiB di picco RSS e
10.256.192 byte di temporanei campionati. Anche fra RAM e SQLite
i sette file sono risultati identici.

Nella [validazione nazionale 45772475295](https://github.com/lcricc/enduro-weather-engine/actions/runs/45772475295)
il validatore ha elaborato 31.184.051 righe di osservazioni e 18.078.048
previsioni da 10 file, con 3.041.817 eventi comuni. Il processo ha impiegato
18 min 53,60 s, con 8.928.796 KiB di picco RSS (8,52 GiB). I temporanei dei
32 blocchi ammontavano a 1.500.698.087 byte a fine scrittura. Tutti i blocchi
passavano per la RAM. L'unione storica separata ha impiegato 27 min 7,54 s,
con 61.864 KiB di picco RSS. La durata della run dal trigger al completamento
è stata 65 min 10 s e include l'attesa prima dell'avvio. Le misure locali
sopra e la run nazionale hanno volumi differenti.

La [run 45781639172](https://github.com/lcricc/enduro-weather-engine/actions/runs/45781639172),
avviata il 26 novembre alle 09:15:31 italiane, è riuscita alle 09:54:34.
Durata complessiva 39 min 03 s. Il validatore ha impiegato **14 min 59,51 s**
e raggiunto **9.030.132 KiB di RSS** (8,61 GiB). Ha letto 31.556.585
righe osservative e 18.078.048 righe previsionali da 10 file, con
3.095.562 eventi comuni e 1.391.225 eventi di pioggia. I 32 blocchi
in RAM totalizzavano 1.527.212.095 byte su disco a fine scrittura.
L'unione storica ha impiegato 20 min 55,33 s, con 61.564 KiB di picco RSS.
Il [rapporto scientifico della run](https://github.com/lcricc/enduro-weather-engine/actions/runs/45781639172/artifacts/15099764608)
conserva i risultati scientifici, mentre tempo e RAM provengono dai log.

### Controlli automatici e soglie

`ENDURO Validator Regression` esegue il test di parità per RAM e SQLite
sulle modifiche al validatore e alle sue dipendenze. Un secondo job esegue
il campione storico con il commit ottimizzato fissato e il candidato in
modalità automatica e con SQLite forzato. Le quattro corse sono sequenziali
sullo stesso runner.
L'artifact conserva i profili e i file scientifici per 90 giorni.

Il controllo fallisce per un incremento del picco RSS **oltre il 12%**
in una delle due modalità rispetto al riferimento della stessa modalità.
Conteggi,
abbinamenti, pioggia, diagnostica e struttura degli output devono coincidere.
La sola tolleranza è **0,0001 assoluto** su MAE, RMSE e bias, una cifra
pubblicata. Tempo e spazio temporaneo sono registrati per osservare
i cambiamenti. L'assenza di un archivio fissato, un checksum diverso
o una verifica senza eventi comuni fa fallire il job.

```bash
python tests/compare_meteohub_benchmarks.py \
  --baseline ddf0d0d9bb308ff62a68666dea0d51d9ef344023 \
  --cache /tmp/meteohub-release-assets --output /tmp/meteohub-historical-initial
```

Omettere `--baseline` per usare il commit ottimizzato anche in modalità
automatica. SQLite usa sempre il commit ottimizzato come riferimento. Per
ripetere il benchmark senza rete dopo il primo recupero, aggiungere
`--offline` e usare una nuova directory di output.

La validazione nazionale si avvia a seguito dell'archiviazione, o manualmente.
La Job Summary e `run_profile.json` riportano durata, picco RSS, occupazione
temporanea campionata del validatore, fase finale, partizioni ed eventi
principali. La calibrazione storica ricalcola i propri risultati e li consegna
come artifact al job di pubblicazione, che effettua solo l'aggiornamento
versionato. L'analisi dell'accuratezza riusa il rapporto della validazione.

### Verifica di un unico indice SQLite

Su quegli stessi archivi abbiamo indicizzato insieme 430.500 valori esatti e
vicini per rete, stazione, ora, variabile e scarto. L'indice in un file
SQLite occupava 34.078.720 byte e richiedeva 1,07 s di costruzione. Le
19.008 ricerche confrontate producevano gli stessi risultati, inclusi
3.362 abbinamenti. Le ricerche hanno impiegato 0,013 s in RAM e 0,162 s
in SQLite con cache da 8 MiB. Il prototipo trasferiva al disco le misure
già caricate e non interveniva sul picco nella lettura delle osservazioni.
Non è stato adottato. I confronti fra modelli continuano a elaborare una
partizione alla volta.
