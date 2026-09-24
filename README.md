# 💧 GPX Drinkwaterpunten

Standalone webapplicatie (Docker) die een GPX-fietsroute inleest, automatisch
drinkwaterpunten langs de route zoekt en een nieuwe GPX genereert met waypoints
die direct bruikbaar zijn op een **Wahoo ELEMNT ROAM 3**.

Routes kunnen via een upload komen of rechtstreeks uit je **Strava**-account.

![stack](https://img.shields.io/badge/python-3.12-blue) ![stack](https://img.shields.io/badge/FastAPI-0.115-009688)

---

## Snelstart

```bash
docker compose build
docker compose up -d
```

Open daarna: `http://<server-ip>:8080`

De container heet `gpx-drinkwaterpunten` en maakt bij de eerste start zelf
`./data/cache` en `./data/tmp` aan.

Logs volgen:

```bash
docker compose logs -f
```

Stoppen:

```bash
docker compose down
```

---

## Hoe het werkt

1. **Upload** een GPX met een `<trk>` (track) of `<rte>` (route). Hoogtegegevens
   en metadata blijven behouden.
2. **Bronkeuze** gebeurt automatisch:
   - ligt meer dan 80% van de routepunten in Nederland → dataset van
     [drinkwaterpunten.nl](https://drinkwaterpunten.nl) (~3.100 punten);
   - anders → OpenStreetMap via de Overpass API
     (`amenity=drinking_water`, `man_made=water_tap`, `drinking_water=yes`).
   De bron kan in de interface ook handmatig geforceerd worden.
3. **Matching**: per waterpunt wordt de loodrechte afstand tot de route en de
   positie langs de route berekend. Punten buiten de zoekradius vervallen,
   punten binnen 50 m van elkaar worden ontdubbeld, de rest wordt gesorteerd op
   rijrichting.
4. **Analyse**: totale afstand, aantal punten, gemiddelde afstand tussen punten,
   langste stuk zonder water en een waarschuwing bij meer dan 40 km droog.
5. **Download**: nieuwe GPX = originele route + drinkwater-waypoints.

De Nederlandse dataset wordt automatisch gedownload en **maximaal eens per 24 uur**
ververst. De cache staat in het volume `./data/cache` en overleeft updates van de
container. Is de bron tijdelijk onbereikbaar, dan wordt de bestaande cache gebruikt.

---

## Webinterface

Drie pagina's:

| Pagina | Route | Inhoud |
|---|---|---|
| GPX upload | `/` | zelf een GPX-bestand aanleveren |
| Strava routes | `/strava` | routes uit je Strava-account ophalen en verwerken |
| Routeboek routes | `/routeboek` | routes van een routeboek.cc-clubpagina ophalen en verwerken |

### Pagina "GPX upload"

- GPX-uploadknop
- keuze zoekradius: 100 / 250 / 500 / 750 / 1000 meter (standaard 250)
- keuze databron (automatisch / NL / OSM)
- optie "Controleer op wegwerkzaamheden" met datumkiezer (standaard vandaag)
- optie "Controleer op regen" met vertrektijd en snelheid (standaard 30 km/u)
- startknop
- na verwerking: interactieve Leaflet-kaart met route en waterpunten,
  analysegegevens en een downloadknop

### Pagina "Strava routes"

- knop **Verbind met Strava** (OAuth2)
- lijst met je routes: naam, afstand, hoogtemeters en of de route privé is
- zoekveld om te filteren op naam
- checkbox-selectie (één of meerdere routes, ook "alles aan/uit")
- keuze zoekradius en databron
- optie "Controleer op wegwerkzaamheden" met datumkiezer (standaard vandaag)
- optie "Controleer op regen" met vertrektijd en snelheid (standaard 30 km/u)
- knop **Maak waterpunten GPX**
- per route het resultaat met twee downloads: het origineel
  (`Routenaam.gpx`) en de verrijkte versie (`Routenaam_waterpunten.gpx`),
  plus alle routes en waterpunten op de kaart

### Pagina "Routeboek routes"

- lijst met routes van een routeboek.cc-clubpagina (standaard
  `routeboek.cc/club/stampers`, instelbaar via `ROUTEBOEK_CLUB_SLUG`): naam,
  afstand en hoogtemeters
- zoekveld om te filteren op naam
- checkbox-selectie, keuze zoekradius/databron en dezelfde opties als de
  andere pagina's
- knop **Maak waterpunten GPX**, met dezelfde resultaatweergave als de
  Strava-pagina

Er is geen officiële routeboek.cc-API: de routelijst wordt gelezen uit de
HTML van de clubpagina (kort gecached, standaard 15 minuten) en de GPX wordt
per geselecteerde route rechtstreeks gedownload op het moment dat je op
"Maak waterpunten GPX" klikt. Ontbreekt het GPX-bestand op routeboek.cc
(gemeten: 1 van de 166 Stampers-routes geeft een 404), dan wordt de route
opgebouwd uit de kaartcoördinaten op de detailpagina — zonder hoogtegegevens,
afstand wijkt ~0,1% af. Er wordt bewust **niet** periodiek alle media
lokaal gesynchroniseerd: dat zou onnodig veel downloads en belasting op
routeboek.cc geven, terwijl de meeste bezoekers maar een handjevol routes
verwerken. Zet `ROUTEBOEK_ENABLED=false` om deze pagina helemaal uit te
schakelen (bijvoorbeeld op een publieke installatie die niet bij jouw club
hoort).

Alle drie de pagina's gebruiken exact dezelfde waterpunten-, wegwerkzaamheden-
en verboden-paden-services; er is geen dubbele logica.

---

## Strava koppelen

Strava is volledig optioneel. Zie [Strava aan- of uitzetten](#strava-aan-of-uitzetten)
als je de module helemaal niet wilt gebruiken.

Er zijn twee manieren. **Optie B** is het snelst als je al een Strava-app met
tokens hebt.

| | Optie A: koppelen via de knop | Optie B: bestaand token |
|---|---|---|
| Nodig | client id + secret + redirect URI | client id + secret + refresh token |
| Callback domain instellen | ja | nee |
| Meerdere gebruikers | ja | nee (één vast account) |

### 1. Strava developer app aanmaken

1. Ga naar <https://www.strava.com/settings/api> en log in.
2. Maak een applicatie aan (*Create & Manage Your App*):
   - **Application Name**: bijvoorbeeld `GPX Drinkwaterpunten`
   - **Category**: `Data Importer` (of iets passends)
   - **Website**: `http://<server-ip>:8080`
   - **Authorization Callback Domain**: **alleen de hostnaam, zonder
     `http://`, zonder poort en zonder pad**, bijvoorbeeld `192.168.1.10`,
     `fiets.example.com` of `localhost`.
3. Noteer de **Client ID** en het **Client Secret**.

### 2A. OAuth callback instellen (optie A)

De applicatie luistert op `"/strava/callback"`. Zet `STRAVA_REDIRECT_URI` op de
volledige URL waarmee jij de app benadert, bijvoorbeeld:

```
STRAVA_REDIRECT_URI=http://192.168.1.10:8080/strava/callback
```

De hostnaam in deze URL moet gelijk zijn aan de *Authorization Callback Domain*
in je Strava-app, anders weigert Strava met `redirect_uri mismatch`.

> **Je maakt bij Strava géén redirect URI aan.** Strava kent alleen het veld
> *Authorization Callback Domain* met daarin uitsluitend de hostnaam. De
> volledige redirect URI bepaal je zelf in `STRAVA_REDIRECT_URI`; die moet
> eindigen op `/strava/callback` en dezelfde hostnaam gebruiken.

### 2B. Bestaand token hergebruiken (optie B)

Heb je al een Strava-app met een **client id**, **client secret** en een
**refresh token**? Zet die dan rechtstreeks in `.env`:

```ini
STRAVA_CLIENT_ID=123456
STRAVA_CLIENT_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
STRAVA_REFRESH_TOKEN=yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy
STRAVA_ACCESS_TOKEN=                # optioneel; wordt toch automatisch ververst
```

`STRAVA_REDIRECT_URI` en het callback domain zijn dan **niet nodig**: de app
haalt met het refresh token zelf een geldig access token op en ververst dat
automatisch. De Strava-pagina is meteen verbonden en toont "vast token uit de
configuratie"; de knop *Koppeling verbreken* verdwijnt.

Het access token uit `.env` mag verlopen zijn — alleen het **refresh token**
moet kloppen. Werkt het niet, dan is het refresh token ingetrokken (bijvoorbeeld
doordat je opnieuw autoriseerde met een andere app) en moet je een nieuw token
ophalen.

> **Scope van een bestaand token:** een token dat ooit voor iets anders is
> aangemaakt heeft vaak niet de scope `read_all`. Je ziet dan alleen je
> *openbare* routes. De pagina waarschuwt hiervoor. Voor privéroutes moet je het
> token opnieuw autoriseren met `read,read_all` (bijvoorbeeld één keer via optie
> A, of met Strava's eigen OAuth-URL).

### 3. Environment variables instellen

Maak een `.env` naast `docker-compose.yml` (zie `.env.example`):

```bash
cp .env.example .env
```

```ini
STRAVA_CLIENT_ID=123456
STRAVA_CLIENT_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
STRAVA_REDIRECT_URI=http://192.168.1.10:8080/strava/callback   # optie A
STRAVA_REFRESH_TOKEN=                                          # optie B
STRAVA_SCOPE=read,read_all
SECRET_KEY=            # leeg laten = automatisch gegenereerd in ./data/secret.key
COOKIE_SECURE=false    # op true zetten achter HTTPS
```

Daarna:

```bash
docker compose up -d
```

Open `/strava`. Bij optie A klik je op **Verbind met Strava**; bij optie B ben je
al verbonden. Zonder deze variabelen blijft
de rest van de applicatie gewoon werken; de Strava-pagina meldt dan dat de
integratie niet is geconfigureerd.

> **Scope:** `read_all` is nodig om ook je *privéroutes* te kunnen zien en
> exporteren. Met alleen `read` toont Strava uitsluitend openbare routes.

### Tokenopslag

- Access- en refresh token staan **versleuteld** (Fernet, AES-128 + HMAC) in
  `./data/strava_tokens.enc`, bestandsrechten `0600`.
- De sleutel komt uit `SECRET_KEY` of uit het automatisch aangemaakte
  `./data/secret.key` (`0600`).
- De browser krijgt alleen een ondertekende, `HttpOnly` sessiecookie met een
  willekeurig sessie-id — nooit het token zelf.
- Verlopen access tokens worden automatisch ververst met het refresh token.
- Sessies verlopen na `SESSION_TTL_SECONDS` (standaard 30 dagen); met
  **Koppeling verbreken** worden de tokens direct verwijderd.

---

## Strava aan- of uitzetten

De Strava-module heeft één schakelaar: `STRAVA_ENABLED`.

| Waarde | Gedrag |
|---|---|
| `auto` (standaard) | Aan zodra `STRAVA_CLIENT_ID` én `STRAVA_CLIENT_SECRET` gevuld zijn |
| `false` | Volledig uit |
| `true` | Geforceerd aan (handig om configuratiefouten zichtbaar te maken) |

Staat de schakelaar op `false`, dan:

- verdwijnt de navigatielink "Strava routes";
- geven `/strava`, `/strava/connect`, `/strava/callback` en alle
  `/api/strava/*` endpoints een **404**;
- worden `STRAVA_ACCESS_TOKEN` en `STRAVA_REFRESH_TOKEN` genegeerd, ook als ze
  toevallig in de omgeving staan.

De applicatie werkt dan als pure GPX-uploadtool. Dat is de aanbevolen stand voor
een installatie die je met anderen deelt.

## Publieke installatie naast je eigen instantie

Je kunt twee containers naast elkaar draaien vanuit dezelfde broncode: een privé
instantie mét jouw Strava-koppeling en een publieke instantie zonder.

| | Privé | Publiek |
|---|---|---|
| Map | `~/gpx` | `/opt/watergpx` |
| Container | `gpx-drinkwaterpunten` | `watergpx-public` |
| Image | `gpx-waterpoints:latest` | `watergpx-public:latest` |
| Poort | 8080 | 8081 |
| `ROADWORKS_ENABLED` | `true` | Controle op wegwerkzaamheden aan/uit |
| `ROADWORKS_LINE_TOLERANCE_M` | `25` | Maximale afstand van het afgesloten stuk tot de route |
| `ROADWORKS_RADIUS_M` | `100` | Terugval voor meldingen zonder afgesloten stuk: afstand van het punt tot de route |
| `ROADWORKS_CACHE_TTL_SECONDS` | `86400` | Verversfrequentie NDW-feed |
| `ROADWORKS_URL` | NDW planningsfeed | Bron-URL |
| `ROADWORKS_MERGE_GAP_M` | `250` | Opeenvolgende meldingen van dezelfde klus binnen deze afstand worden één waarschuwing |
| `BACKGROUND_REFRESH` | `true` | Caches (drinkwaterpunten.nl, NDW) op de achtergrond vers houden |
| `BACKGROUND_REFRESH_INTERVAL_SECONDS` | `3600` | Hoe vaak de achtergrondlus controleert of een cache verouderd is |
| `LEGALITY_ENABLED` | `true` | Controle op verboden paden aan/uit (uit = geen kaartdownload) |
| `OSM_PBF_URL` | Geofabrik Nederland | Bron van de wegenkaart |
| `OSM_MAX_AGE_DAYS` | `30` | Na zoveel dagen wordt de wegenkaart opnieuw opgebouwd |
| `OSM_MIN_FREE_MB` | `2800` | Minimaal vrij geheugen om met opbouwen te beginnen |
| `OSM_START_JITTER_SECONDS` | `1800` | Maximale willekeurige wachttijd voor de eerste kaartcontrole |
| `LEGALITY_PREFIX_FORBIDDEN` | `⛔ Verboden` | Waypointnaam voor verboden stukken |
| `LEGALITY_PREFIX_WARNING` | `❗ Let op` | Waypointnaam voor let-op-stukken |
| `LEGALITY_SYM` | `Danger Area` | GPX `<sym>` voor beide |
| `STRAVA_ENABLED` | `auto` (aan) | `false` |
| `.env` met tokens | ja | **nee** |

Opzetten:

```bash
sudo mkdir -p /opt/watergpx && sudo chown "$USER" /opt/watergpx
rsync -a --exclude=.git --exclude=.venv --exclude=data --exclude=.env ~/gpx/ /opt/watergpx/
cd /opt/watergpx && docker compose up -d --build
```

De publieke `docker-compose.yml` zet `STRAVA_ENABLED: "false"` hard en bevat
géén Strava-variabelen. Zet in die map dus nooit een `.env` met credentials neer.

### Waarom Strava niet zomaar publiek kan

Draai je één instantie met jouw eigen refresh token en zet je die publiek, dan
gebruikt **iedere bezoeker jouw Strava-account**. Wil je derden hun eigen Strava
laten koppelen, dan heb je optie A (OAuth) nodig plus:

- HTTPS met `COOKIE_SECURE=true`;
- Strava's limieten: nieuwe apps mogen **1 atleet**, na een self-service upgrade
  in het API-dashboard **10 atleten**, en daarboven moet je app door een
  **review** van Strava;
- rate limits gelden **per applicatie** (standaard 100 leesverzoeken per 15
  minuten, 1.000 per dag) en worden dus door al je gebruikers gedeeld;
- Strava's brand guidelines: officiële "Connect with Strava"-knop en
  "Powered by Strava"-attributie;
- een privacyverklaring, omdat je tokens van derden opslaat.

Voor een breed publiek is de uploadpagina daarom de praktische keuze: die kent
geen limieten, geen review en slaat geen persoonsgegevens op.

## Wegwerkzaamheden controleren

Optioneel kun je de route laten controleren op wegwerkzaamheden. Vink
**"Controleer op wegwerkzaamheden"** aan en kies de datum van je rit
(standaard vandaag).

### Bron

De [NDW-planningsfeed](https://opendata.ndw.nu/) met meldingen uit **Melvin**,
het systeem waarin Rijkswaterstaat, provincies én gemeenten hun werkzaamheden
invoeren. Open data, geen sleutel nodig:

```
https://opendata.ndw.nu/planningsfeed_wegwerkzaamheden_en_evenementen.xml.gz
```

Juist doordat gemeenten hier melden, zitten er ook regionale fietspaden in.
De app houdt alleen meldingen over die fietsers raken (`vehicleType` is
`bicycle`, `moped` of `motorscooter`) én een coördinaat hebben: circa 1.900
van de bijna 14.000 situaties in de feed.

DATEX II bundelt per *situatie* meerdere deelmeldingen: de afsluiting zelf en
de bijbehorende omleiding. Alleen het omleidingsrecord bevat coördinaten,
terwijl de oorzaak meestal in het afsluitingsrecord staat. De app leest daarom
op situatieniveau — dat levert een volledige omschrijving op (96% van de
meldingen heeft een oorzaak, tegen 24% bij losse records) en voorkomt
dubbelingen.

Per melding wordt overgenomen:

- oorzaak (bv. "Asfaltering / wegverharding", "Bouwactiviteiten")
- verantwoordelijke instantie (bv. "Gemeente Woerden")
- begin- en einddatum
- de omleidingsomschrijving, indien aanwezig
- het **afgesloten stuk** als lijn (uit het afsluitingsrecord; bij 95% van de meldingen)

### Wanneer ligt een melding op je route?

Bepalend is het **afgesloten stuk zelf**: een melding telt als die lijn binnen
`ROADWORKS_LINE_TOLERANCE_M` (25 m) van je route komt. Het losse punt dat NDW
meestuurt is daarvoor ongeschikt: het staat vaak bij een omleidingsbord of op
een parallelle straat, tot honderden meters van het werk. De lijnen van het
omleidingsrecord worden bewust genegeerd; dat is juist de weg die open is.

Gemeten op de 165 routes van routeboek, met alle meldingen die volgens de oude
regel (punt binnen 250 m) werden getoond:

| Afgesloten stuk tot de route | Aantal |
|---|---|
| 0–10 m | 311 |
| 10–25 m | 23 |
| 25–50 m | 18 |
| meer dan 50 m | 320 |

Bijna de helft lag dus niet op de route. Omgekeerd vindt de app nu ook 28
werkvakken die wél op de route liggen, maar waarvan het punt verder dan 250 m
weg stond.

Op de kaart zie je het afgesloten stuk als oranje stippellijn, en de marker
(en het waypoint in de GPX) staat op de plek waar dat stuk je route raakt.
Voor de ~7% meldingen zonder afgesloten stuk valt de app terug op het punt,
binnen `ROADWORKS_RADIUS_M` (100 m).

### Cache

De feed is ~15 MB gzip (~170 MB XML). Hij wordt gestreamd geparseerd en
teruggebracht tot een compact JSON-bestand in
`data/cache/wegwerkzaamheden_fiets_v2.json` (circa 1.900 meldingen). Dat duurt
ongeveer tien seconden.

Een achtergrondlus houdt deze cache én die van drinkwaterpunten.nl vers: bij
het opstarten en daarna elk uur kijkt hij of een cache ouder is dan zijn TTL
(standaard 24 uur) en ververst dan. Bezoekers wachten dus nooit op een
download, ook niet op een instantie die weken niemand heeft gebruikt. Zet
`BACKGROUND_REFRESH=false` om terug te vallen op verversen bij het eerste
verzoek. Verversen kan ook handmatig:

```bash
curl -X POST http://localhost:8080/api/roadworks/refresh
```

Valt NDW uit, dan gebruikt de app de vorige cache en gaat de verwerking van
drinkwaterpunten gewoon door.

### Beperkingen

- **Alleen Nederland.** Buitenlandse routes krijgen een melding; er is geen
  vergelijkbare Europese bron.
- **Melden is niet verplicht.** Niet elke gemeente voert alles in Melvin in.
  Zie het als waarschuwing vooraf, niet als garantie.
- **Datum is bepalend.** Het merendeel van de meldingen ligt in de toekomst.
  Kies de datum waarop je écht rijdt.
- **Het afgesloten stuk is zo nauwkeurig als de melder het intekent.** Een
  werk op een fietspad direct naast de rijbaan kan binnen 25 m vallen, ook als
  je route over de rijbaan loopt. Verlaag `ROADWORKS_LINE_TOLERANCE_M` als je
  daar last van hebt; verhoog hem als je meldingen mist.

### Samenvoegen van meldingen

Gemeenten melden één project vaak in stukken: per straatdeel, per fase of per
rijrichting. Meldingen met dezelfde oorzaak en instantie die elkaar langs de
route binnen `ROADWORKS_MERGE_GAP_M` (250 m) opvolgen, worden samengevoegd tot
één waarschuwing. Het gat telt vanaf de laatste melding van de reeks, zodat
een lang werkvak niet halverwege breekt. De melding die het dichtst bij de
route ligt blijft over.

### In de GPX

Gevonden werkzaamheden komen als extra waypoints in het bestand:

```xml
<wpt lat="52.078" lon="4.312">
  <name>⚠️ Werkzaamheden - 12 km</name>
  <desc>Asfaltering / wegverharding | Gemeente Woerden | Periode: 2026-08-06 t/m 2026-08-06 | Het verkeer wordt omgeleid via ... | Bron: NDW/Melvin</desc>
  <sym>Danger Area</sym>
  <type>Roadworks</type>
</wpt>
```

Zo zie je ze op je Wahoo naast de drinkwaterpunten. Zet
`ROADWORKS_ENABLED=false` om de functie volledig uit te schakelen.

## Verboden paden controleren

Vink **Controleer op verboden paden** aan (upload- en Stravapagina) en de app
controleert of je route over stukken loopt waar fietsen niet, of niet zonder
meer, mag. Deze functie is overgenomen uit routeboek: de regels en het
samenvoegen van meldingen zijn daar gelijk.

| Soort | Voorbeelden |
|---|---|
| ⛔ Verboden | voetpad, trap, `bicycle=no`, autoweg, privéterrein |
| ❗ Let op | afstappen verplicht, voetgangersgebied, ruiterpad, wandelpad, verplicht fietspad ernaast |

Stoepen en zebrapaden tellen niet mee, en een voetpad naast een gewone weg
ook niet. Alleen een pad waar geen toegestane weg binnen 20 m ligt wordt
gemeld; dat voorkomt valse meldingen door GPS-afwijking. Meldingen met
dezelfde reden die minder dan 250 m uit elkaar liggen worden samengevoegd,
zodat een lange dijk met inritten niet in zeven stukken uiteenvalt.

Elk gemeld stuk komt als waypoint op het **beginpunt** in de GPX, zodat je
Wahoo waarschuwt voordat je het pad op rijdt:

```xml
<wpt lat="52.35" lon="4.9">
  <name>⛔ Verboden - 6 km</name>
  <cmt>Voetpad</cmt>
  <desc>Voetpad | Stadspark | km 5.5 t/m 6.0 (500 m) | Bron: OpenStreetMap</desc>
  <sym>Danger Area</sym>
  <type>Forbidden</type>
</wpt>
```

### De lokale wegenkaart

De controle gebruikt een eigen kopie van de Nederlandse wegen uit
OpenStreetMap, in `data/osm/netherlands.sqlite` (~450 MB). Daardoor duurt een
controle 1 à 2 seconden per 100 km en belast de app geen publieke servers.
Overpass is in routeboek geprobeerd en weer verlaten: 3 tot 5 minuten per
controle, en na een paar controles werd het IP-adres geblokkeerd.

De kaart wordt automatisch opgebouwd bij de eerste start en daarna zodra hij
ouder is dan `OSM_MAX_AGE_DAYS` (30 dagen):

1. Het Nederland-extract van Geofabrik downloaden (~1,4 GB).
2. Met `osmium` alleen de wegen overhouden en omzetten naar GeoJSON.
3. Inlezen in SQLite met een R*Tree-index.

Dat duurt enkele minuten. Tijdens het bouwen blijft de oude kaart in gebruik;
zonder kaart meldt de app netjes dat hij nog wordt opgebouwd, en de
waterpunten worden gewoon verwerkt.

**Geheugen:** `osmium` piekt tijdens het bouwen rond **2,5 GB**. Draaien er
meerdere installaties op één server (privé, publiek, routeboek), dan bouwt
elk zijn eigen kaart. Om te voorkomen dat die tegelijk bouwen:

- wacht de eerste controle na het opstarten een willekeurige tijd (tot
  `OSM_START_JITTER_SECONDS`, standaard 30 minuten);
- begint de opbouw alleen als er minstens `OSM_MIN_FREE_MB` (2800 MB) vrij is,
  anders wordt het een uur later opnieuw geprobeerd.

Status en handmatig opbouwen:

```bash
curl http://localhost:8080/api/osm/status
curl -X POST http://localhost:8080/api/osm/refresh
```

Zet `LEGALITY_ENABLED=false` om de functie volledig uit te zetten; er wordt
dan ook geen kaart gedownload.

### Beperkingen

- **Alleen Nederland.**
- **OpenStreetMap is zo goed als de tagging.** In Nederland is die zeer
  nauwkeurig, maar een net geopend of juist afgesloten pad kan nog ontbreken.
- `highway=path` zonder verdere tags wordt bewust niet gemeld; dat is in
  Nederland te dubbelzinnig.

## Regen onderweg controleren

Vink **Controleer op regen** aan, kies een vertrektijd en je gemiddelde
snelheid (standaard 30 km/u, instelbaar van 5 tot 60 km/u, standaard via
`DEFAULT_SPEED_KMH`). De app berekent voor elke kilometer hoe laat je daar
bent en welke neerslag er op dat moment op die plek verwacht wordt.

### Bronnen

| Periode na nu | Bron | Resolutie |
|---|---|---|
| 0 – 2 uur (alleen NL) | Buienradar-radarverwachting (`gpsgadget.buienradar.nl/data/raintext`) | 5 min |
| tot ~2,5 dag | KNMI Harmonie-model, via Open-Meteo (`models=knmi_seamless`) | 15 min, ~2,5 km |
| verder, of buiten West-Europa | ECMWF (automatische terugval van Open-Meteo) | grover, minder betrouwbaar |

Het KNMI Data Platform zelf vereist een API-sleutel; Open-Meteo levert
hetzelfde KNMI-model zonder sleutel. Beide bronnen zijn gratis voor
niet-commercieel gebruik. Een route van 100 km kost één Open-Meteo-verzoek
(alle meetpunten in één keer, ~0,3 s) plus, bij direct vertrek, enkele
Buienradar-verzoeken. Bij tijdelijke drukte (HTTP 429/503) wordt het
verzoek tot twee keer herhaald.

### Uitkomst

- samenvatting: **droog verwacht** of het aantal natte stukken;
- vertrek- en aankomsttijd, en de hoogste kans op neerslag onderweg;
- een tijdlijnbalk van start tot finish (grijs = droog, lichtblauw = licht,
  blauw = matig, donkerblauw = zwaar; hover voor km, tijd en mm/u);
- natte stukken als blauwe band op de kaart, met km, tijdstip, intensiteit
  en bron;
- in de GPX een waypoint `🌧️ Regen - 42 km` aan het begin van elk nat stuk
  (type `Weather`), met tijdstip, intensiteit en kans in de omschrijving.

Een stuk telt als nat vanaf 0,1 mm/u (`WEATHER_RAIN_THRESHOLD_MM_H`); natte
kilometers die minder dan 2 km uit elkaar liggen worden één stuk.
Intensiteit: < 1 mm/u licht, 1–4 mm/u matig, > 4 mm/u zwaar.

**Mogelijk regen.** Soms rekent het model regen terwijl de kans op neerslag
voor dat uur klein is — vooral bij ECMWF een paar dagen vooruit (gemeten:
"matige regen 2,4 mm/u" bij 12% kans). Zo'n stuk (kans onder
`WEATHER_UNCERTAIN_PROBABILITY`, standaard 30%) wordt getoond als
**mogelijk regen**: gestippeld op de kaart, `🌦️ Mogelijk regen - 42 km` in de
GPX, en de samenvatting wordt "waarschijnlijk droog" als er alleen zulke
stukken zijn. Radarwaarnemingen zijn nooit onzeker.

### Beperkingen

- Constante snelheid: pauzes, wind en hoogteverschil worden niet
  meegerekend. Plan je een koffiestop, reken dan met een iets lagere
  gemiddelde snelheid.
- Een weersverwachting verandert: de waypoints in de GPX gelden voor de
  verwachting op het moment van genereren. Controleer vlak voor vertrek
  opnieuw.
- Buien zijn lokaal; op modelschaal (2,5 km, 15 min) kan een bui net naast
  of net anders vallen dan voorspeld.
- Maximaal `WEATHER_MAX_DAYS` (7) dagen vooruit.

---

## Wahoo ELEMNT ROAM 3

De gegenereerde waypoints zijn standaard GPX 1.1 `<wpt>`-elementen op
rootniveau — precies wat de ELEMNT-firmware inleest:

```xml
<wpt lat="52.089393" lon="5.109821">
  <name>💧 Water - 12 km</name>
  <cmt>Utrecht Centraal</cmt>
  <desc>Utrecht Centraal | Beheerder: Gemeente | Open: 24/7 | Afstand tot route: 69 m | Bron: drinkwaterpunten.nl</desc>
  <link href="https://drinkwaterpunten.nl?id=45198301"><text>info</text></link>
  <sym>Water Source</sym>
  <type>Water</type>
</wpt>
```

Overzetten naar de ROAM 3: kopieer het bestand via de ELEMNT-app
(Routes → importeren) of plaats het in de map `plans/` van het apparaat.

Tip: gebruikt jouw firmware liever een ander symbool of geen emoji, pas dan
`WAYPOINT_SYM` / `WAYPOINT_PREFIX` / `WAYPOINT_WITH_KM` aan in
`docker-compose.yml`.

---

## Configuratie (environment variables)

| Variabele | Standaard | Betekenis |
|---|---|---|
| `PORT` | `8080` | Poort in de container |
| `LOG_LEVEL` | `INFO` | Logniveau (stdout) |
| `DATA_DIR` | `/app/data` | Map voor cache en tijdelijke bestanden |
| `NL_GPX_URL` | drinkwaterpunten.nl GPX | Bron Nederlandse dataset |
| `NL_CACHE_TTL_SECONDS` | `86400` | Cachegeldigheid (24 uur) |
| `OVERPASS_URL` | overpass-api.de, private.coffee | Overpass endpoints, komma-gescheiden; bij falen wordt de volgende geprobeerd |
| `OVERPASS_TIMEOUT` | `60` | Timeout per Overpass-query (s) |
| `DEFAULT_RADIUS_M` | `250` | Standaard zoekradius (keuze: 100/250/500/750/1000) |
| `DEDUPE_DISTANCE_M` | `50` | Ontdubbelafstand |
| `NL_SHARE_THRESHOLD` | `0.8` | Drempel voor NL-bron |
| `GAP_WARNING_KM` | `40` | Waarschuwingsgrens droog stuk |
| `MAX_UPLOAD_MB` | `25` | Maximale uploadgrootte |
| `JOB_TTL_SECONDS` | `21600` | Bewaartijd gegenereerde GPX-bestanden |
| `WAYPOINT_PREFIX` | `💧 Water` | Basisnaam van waypoints |
| `WAYPOINT_WITH_KM` | `true` | Kilometerstand in de naam |
| `WAYPOINT_SYM` | `Water Source` | GPX `<sym>` |
| `WAYPOINT_TYPE` | `Water` | GPX `<type>` |
| `ROADWORKS_ENABLED` | `true` | Controle op wegwerkzaamheden aan/uit |
| `ROADWORKS_LINE_TOLERANCE_M` | `25` | Maximale afstand van het afgesloten stuk tot de route |
| `ROADWORKS_RADIUS_M` | `100` | Terugval voor meldingen zonder afgesloten stuk: afstand van het punt tot de route |
| `ROADWORKS_CACHE_TTL_SECONDS` | `86400` | Verversfrequentie NDW-feed |
| `ROADWORKS_URL` | NDW planningsfeed | Bron-URL |
| `ROADWORKS_MERGE_GAP_M` | `250` | Opeenvolgende meldingen van dezelfde klus binnen deze afstand worden één waarschuwing |
| `BACKGROUND_REFRESH` | `true` | Caches (drinkwaterpunten.nl, NDW) op de achtergrond vers houden |
| `BACKGROUND_REFRESH_INTERVAL_SECONDS` | `3600` | Hoe vaak de achtergrondlus controleert of een cache verouderd is |
| `LEGALITY_ENABLED` | `true` | Controle op verboden paden aan/uit (uit = geen kaartdownload) |
| `OSM_PBF_URL` | Geofabrik Nederland | Bron van de wegenkaart |
| `OSM_MAX_AGE_DAYS` | `30` | Na zoveel dagen wordt de wegenkaart opnieuw opgebouwd |
| `OSM_MIN_FREE_MB` | `2800` | Minimaal vrij geheugen om met opbouwen te beginnen |
| `OSM_START_JITTER_SECONDS` | `1800` | Maximale willekeurige wachttijd voor de eerste kaartcontrole |
| `LEGALITY_PREFIX_FORBIDDEN` | `⛔ Verboden` | Waypointnaam voor verboden stukken |
| `LEGALITY_PREFIX_WARNING` | `❗ Let op` | Waypointnaam voor let-op-stukken |
| `LEGALITY_SYM` | `Danger Area` | GPX `<sym>` voor beide |
| `STRAVA_ENABLED` | `auto` | `auto` = aan zodra client id en secret zijn ingevuld, `false` = module volledig uit, `true` = geforceerd aan |
| `STRAVA_CLIENT_ID` | – | Client ID van je Strava-app |
| `STRAVA_CLIENT_SECRET` | – | Client Secret van je Strava-app |
| `STRAVA_REDIRECT_URI` | – | Optie A: volledige callback-URL, eindigend op `/strava/callback` |
| `STRAVA_REFRESH_TOKEN` | – | Optie B: bestaand refresh token; koppelen via de knop is dan niet nodig |
| `STRAVA_ACCESS_TOKEN` | – | Optie B: optioneel startpunt, wordt automatisch ververst |
| `STRAVA_SCOPE` | `read,read_all` | OAuth-scope (`read_all` = ook privéroutes) |
| `STRAVA_MAX_ROUTES` | `100` | Maximum aantal op te halen routes |
| `SECRET_KEY` | – | Sleutel voor cookies en tokenversleuteling (leeg = auto) |
| `COOKIE_SECURE` | `false` | Zet op `true` achter HTTPS |
| `SESSION_TTL_SECONDS` | `2592000` | Levensduur van een sessie (30 dagen) |
| `WEATHER_ENABLED` | `true` | Regencontrole aan/uit |
| `DEFAULT_SPEED_KMH` | `30` | Voorgestelde gemiddelde snelheid |
| `WEATHER_RAIN_THRESHOLD_MM_H` | `0.1` | Vanaf deze intensiteit telt een kilometer als nat |
| `WEATHER_MAX_DAYS` | `7` | Hoe ver vooruit een vertrektijd mag liggen |
| `WEATHER_UNCERTAIN_PROBABILITY` | `30` | Regen met een lagere kans (%) heet "mogelijk regen" |
| `WEATHER_PREFIX_UNCERTAIN` | `🌦️ Mogelijk regen` | Waypointnaam voor onzekere natte stukken |
| `WEATHER_MODEL` | `knmi_seamless` | Open-Meteo-model |
| `WEATHER_API_URL` | Open-Meteo forecast | Bron modelverwachting |
| `WEATHER_RADAR_URL` | Buienradar raintext | Bron radarverwachting |
| `WEATHER_TIMEZONE` | `Europe/Amsterdam` | Tijdzone van de ingevoerde vertrektijd |
| `WEATHER_PREFIX` | `🌧️ Regen` | Waypointnaam voor natte stukken |
| `WEATHER_SYM` | `Danger Area` | GPX `<sym>` voor natte stukken |
| `ROUTEBOEK_ENABLED` | `true` | Pagina "Routeboek routes" aan/uit |
| `ROUTEBOEK_BASE_URL` | `https://routeboek.cc` | Basis-URL van de bronsite |
| `ROUTEBOEK_CLUB_SLUG` | `stampers` | Clubnaam in de URL (`routeboek.cc/club/<slug>`) |
| `ROUTEBOEK_CACHE_TTL_SECONDS` | `900` | Hoe lang de routelijst in het geheugen gecached blijft (15 minuten) |

Zie `.env.example`. Een `.env` in de projectmap wordt automatisch geladen.

---

## API

| Endpoint | Methode | Omschrijving |
|---|---|---|
| `/` | GET | Webinterface |
| `/api/process` | POST | multipart: `file`, `radius`, `source`, `roadworks`, `ride_date`, `legality`, `weather`, `departure` (`JJJJ-MM-DDTUU:MM`), `speed_kmh` → JSON met route, waterpunten en statistiek |
| `/api/download/{job_id}` | GET | Gegenereerde GPX |
| `/api/cache/refresh` | POST | Forceer verversen NL-dataset |
| `/api/roadworks/refresh` | POST | Forceer verversen NDW-wegwerkzaamheden |
| `/api/routeboek/routes` | GET | Routes van de routeboek.cc-clubpagina (`?refresh=1` forceert een nieuwe scrape) |
| `/api/routeboek/process` | POST | JSON: `route_ids`, `radius`, `source`, `roadworks`, `ride_date`, `legality` → resultaat per route |
| `/strava` | GET | Pagina met Strava-routes |
| `/strava/connect` | GET | Start OAuth-koppeling |
| `/strava/callback` | GET | OAuth-callback van Strava |
| `/api/strava/status` | GET | Configuratie- en koppelstatus |
| `/api/strava/routes` | GET | Routes van de gekoppelde atleet |
| `/api/strava/process` | POST | JSON: `route_ids`, `radius`, `source`, `roadworks`, `ride_date` → resultaat per route |
| `/api/strava/disconnect` | POST | Koppeling en tokens verwijderen |
| `/api/health` | GET | Status + cacheleeftijd |
| `/docs` | GET | OpenAPI-documentatie |

Voorbeeld:

```bash
curl -s -F "file=@rit.gpx" -F "radius=250" -F "source=auto" \
  http://localhost:8080/api/process | jq '.stats'

# met controle op wegwerkzaamheden voor een specifieke datum
curl -s -F "file=@rit.gpx" -F "radius=250" -F "roadworks=true" \
  -F "ride_date=2026-09-12" http://localhost:8080/api/process | jq '.road_works'

curl -sOJ "http://localhost:8080/api/download/<job_id>?name=rit-water.gpx"
```

---

## Projectstructuur

```
app/
  main.py                  applicatie-opbouw, routers inladen
  config.py                settings via environment variables
  web.py                   gedeelde Jinja2-omgeving
  routers/
    core.py                upload, verwerking, download, health
    strava.py               OAuth, routelijst, batchverwerking
    routeboek.py            routeboek.cc: routelijst, batchverwerking
  models/schemas.py        datastructuren (RoutePoint, WaterPoint, RouteStats, Strava*, Routeboek*)
  services/
    gpx_service.py         GPX lezen/schrijven, waypoints bouwen
    waterpoints_nl.py      drinkwaterpunten.nl + 24-uurs cache
    roadworks_nl.py        NDW/Melvin wegwerkzaamheden + 24-uurs cache
    refresher.py           houdt de caches op de achtergrond vers
    osm_index.py           lokale OSM-wegenkaart (SQLite + R*Tree), maandelijks opgebouwd
    legality.py            controle op verboden paden (overgenomen uit routeboek)
    osm_service.py         Overpass API
    route_service.py       afstand, positie langs route, ontdubbelen, statistiek
    geo.py                 projectie, NL-detectie, bounding box
    processing.py          orkestratie upload → resultaat
    strava_service.py      Strava OAuth2 en API-aanroepen
    routeboek_service.py   routeboek.cc scrapen (routelijst + GPX-download)
    weather_service.py     regencontrole: KNMI Harmonie (Open-Meteo) + Buienradar
    token_store.py         versleutelde tokenopslag
  templates/{index.html,strava.html,routeboek.html,_weather_option.html}
  static/{style.css,app.js,strava.js,routeboek.js,legality.js,weather.js}
tests/
Dockerfile, entrypoint.sh, docker-compose.yml, requirements.txt
```

---

## Lokaal ontwikkelen

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest httpx
DATA_DIR=./data .venv/bin/uvicorn app.main:app --reload --port 8080
```

Tests (draaien volledig offline):

```bash
.venv/bin/python -m pytest -q
```

Gedekt: GPX-upload en -parsing (tracks én routes), laden van de Nederlandse
waterpunten inclusief cachegedrag en fallback, afstands- en positieberekening,
ontdubbelen, statistiek/waarschuwing, genereren en teruglezen van de nieuwe GPX,
alle API-endpoints, en de volledige Strava-flow (OAuth met state-controle,
versleutelde tokenopslag, tokenvernieuwing, routelijst, batchverwerking en
foutafhandeling per route).

---

## Aandachtspunten

- Overpass API is een gratis dienst met rate limits; routes buiten Nederland
  kunnen daardoor traag zijn (soms enkele minuten). Er zijn twee endpoints
  ingesteld; faalt de eerste, dan wordt automatisch de tweede gebruikt.
- De container draait als niet-root gebruiker. `entrypoint.sh` zet eenmalig de
  eigenaar van `./data` goed en laat daarna de root-rechten vallen, zodat het
  bind-mounted volume altijd schrijfbaar is.
- Draait Docker bij jou alleen met verhoogde rechten, gebruik dan
  `sudo docker compose up -d`.
- De NL/buitenland-detectie gebruikt een vereenvoudigde landsomtrek; bij routes
  vlak langs de grens kun je de bron handmatig kiezen.
- Strava hanteert rate limits (standaard 100 aanvragen per 15 minuten); verwerk
  daarom niet te veel routes tegelijk (maximaal 25 per keer).
- **Afstandsberekening:** de app rekent bolvormig (great-circle), net als Strava,
  Garmin en Wahoo, zodat de getoonde afstand overeenkomt met wat je in die apps
  ziet. Een WGS84-ellipsoïde zou op Nederlandse breedtegraad ~0,2% hoger
  uitkomen. De route in de GPX wordt nooit gewijzigd: er komen alleen `<wpt>`
  waypoints bij, de trackpunten en hoogtes blijven exact gelijk.
- Controleer drinkwaterpunten onderweg altijd zelf; datasets kunnen verouderd zijn.
