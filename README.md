# Moj Elektro – letna bilanca energije

Integracija za Home Assistant, ki iz uradnega API-ja **Moj Elektro** prebere energetsko bilanco merilnega mesta za tekoče koledarsko leto in tekoči mesec:

- **prejeta energija** (odjem iz omrežja, register A+),
- **oddana energija** (oddaja v omrežje, register A−),
- **neto bilanca** = prejeta − oddana.

Pozitivna bilanca pomeni, da je merilno mesto iz omrežja prejelo več, kot je oddalo. Negativna bilanca pomeni višek oddane energije, kar je pri samooskrbi s sončno elektrarno (na primer SolarEdge) običajen rezultat. Namenjena je znački na nadzorni plošči Dom.

Distributer dnevna stanja objavi z zamikom, običajno približno 24 ur. Značka zato kaže zadnje objavljeno stanje števca, ne trenutne moči razsmernika.

## Senzorji

Ob dodajanju nastanejo stabilni ID-ji, ki se ne ravnajo po jeziku vmesnika. Vezaj v enotnem identifikatorju merilnega mesta (EIMM) se zamenja s podčrtajem. Za EIMM `3-123456`:

| Entiteta | Pomen | Enota | `device_class` | `state_class` |
| --- | --- | --- | --- | --- |
| `sensor.moj_elektro_3_123456_letna_prejeta_energija` | Prejeta energija od 1. januarja | kWh | `energy` | `total` |
| `sensor.moj_elektro_3_123456_letna_oddana_energija` | Oddana energija od 1. januarja | kWh | `energy` | `total` |
| `sensor.moj_elektro_3_123456_letna_bilanca` | Letna bilanca (prejeta − oddana) | kWh | `energy` | brez |
| `sensor.moj_elektro_3_123456_mesecna_prejeta_energija` | Prejeta energija v tekočem mesecu | kWh | `energy` | `total` |
| `sensor.moj_elektro_3_123456_mesecna_oddana_energija` | Oddana energija v tekočem mesecu | kWh | `energy` | `total` |
| `sensor.moj_elektro_3_123456_mesecna_bilanca` | Mesečna bilanca | kWh | `energy` | brez |

Vsak senzor ima atribut `last_reading_date` (datum zadnjega uporabljenega stanja) in `baseline_date` oziroma pri bilanci datuma obeh registrov. Na prejeti in oddani energiji sta tudi surovi stanji števca (`baseline_register_kwh`, `latest_register_kwh`).

`state_class: total` pri prejeti in oddani energiji dopušča, da se vrednost 1. januarja oziroma 1. v mesecu vrne proti nič. Bilanca je lahko negativna. Home Assistant pri `device_class: energy` dovoli samo `total` in `total_increasing`, zato bilanca nima `state_class`. Za ploščo Energija v Home Assistantu so primernejši monotoni števci; ta integracija je namenjena znački bilance.

Imena v vmesniku so prevedena (slovenščina in angleščina). ID entitete ostane tak, kot je v tabeli.

## API

Vsi klici sledijo specifikaciji **Moj elektro API 1.0**:

- dokumentacija: https://docs.informatika.si/mojelektro/api/
- OpenAPI: https://docs.informatika.si/mojelektro/api/mojelektro-openapi.yaml
- produkcija: `https://api.informatika.si/mojelektro/v1`
- testno okolje iz specifikacije (`https://api-test.informatika.si/mojelektro/v1`) integracija ne uporablja

**Avtentikacija:** glava `X-API-TOKEN` (shema `X-API-TOKEN` v OpenAPI, `apiKey` v headerju).

**Uporabljene metode:**

| Metoda | Namen |
| --- | --- |
| `GET /reading-type` | Katalog registrov. Iz njega se vzameta oznaki `A+_T0` in `A-_T0`. |
| `GET /meter-readings` | Dnevna stanja. Parametri: `usagePoint`, `startTime`, `endTime` (`LLLL-MM-DD`), ponovljen `option=ReadingType=...`. |
| `GET /merilno-mesto/{identifikator}` | Preverjanje in ime merilnega mesta, ko je identifikator EIMM. |
| `GET /merilna-tocka/{gsrn}` | Ime merilne točke, ko je `usagePoint` številčni GSRN. |
| `GET /souporaba` | Neobvezno. Če žeton vidi vloge za souporabo, se iz `eiMmOddajnika` in `eiMmPrejemnika` ponudijo EIMM. |

`usagePoint` je po specifikaciji enotni identifikator merilnega mesta, GSRN merilnega mesta ali GSRN merilne točke souporabe. EIMM ima obliko `\d{1,2}-\d+` (na primer `3-123456`).

### Registri

Letna in mesečna energija je razlika dveh **24-urnih stanj** (vrsta `STANJE`), ne vsota 15-minutnih količin. Stanje na začetku obdobja se odšteje od zadnjega stanja.

| Oznaka | Pomen | Vrsta | `readingType` |
| --- | --- | --- | --- |
| `A+_T0` | Prejeta delovna energija ET, 24-urno stanje | `STANJE` | `32.0.4.1.1.2.12.0.0.0.0.0.0.0.0.3.72.0` |
| `A-_T0` | Oddana delovna energija ET, 24-urno stanje | `STANJE` | `32.0.4.1.19.2.12.0.0.0.0.0.0.0.0.3.72.0` |
| `A+` | 15-minutna prejeta energija (primer v OpenAPI, za bilanco se ne sešteva) | `KOLICINA` | `32.0.2.4.1.2.12.0.0.0.0.0.0.0.0.3.72.0` |
| `A-` | 15-minutna oddana energija (navedena v opisu `GET /meter-readings` za souporabo) | `KOLICINA` | `32.0.2.4.19.2.12.0.0.0.0.0.0.0.0.3.72.0` |

Opisi v katalogu pravijo kWh. Koda enote 72 je Wh, množitelj 3 pomeni kilo, kar je ista količina. Vrednosti v odgovoru so nizi (`ReadingValue`) in jih integracija bere kot kWh.

Če 1. januarja ni stanja, se za osnovo vzame prvi dan v oknu 1.–7. januar. Datum osnove je v atributu `baseline_date`. Če v tem oknu ni nobenega stanja, je letni senzor nedosegljiv. Eno samo stanje sredi leta še ni razlika, zato senzor ostane nedosegljiv, dokler ni vsaj dveh dni. Če gre register nazaj (menjava števca), senzor postane nedosegljiv in dobi atribut `register_reset`, namesto da bi pokazal negativno prejeto energijo.

Prazno ali manjkajoče polje `readingQualities` po specifikaciji pomeni veljaven odčitek. Odčitki z oznako kakovosti se vseeno upoštevajo; oznake so v atributu `last_reading_qualities`.

### Okna poizvedb in omejitve

OpenAPI ne zapiše številčne omejitve datuma. Živi API zavrne predolg interval z napako `E_4_000` (*začetek in konec sta lahko največ 35 dni narazen in največ 2 leti v preteklosti*). Integracija zato nikoli ne vpraša za več kot 35 dni:

- od januarja do začetka februarja zadošča en klic od 1. januarja do danes,
- pozneje sta klica dva: 1.–7. januar (osnova leta) in tekoči mesec (zadnje stanje in mesečna bilanca).

Osveževanje je na 6 ur (`iot_class: cloud_polling`). Katalog registrov in opis merilnega mesta se prebereta enkrat na zagon. Uradne omejitve števila klicev specifikacija ne objavi; na forumih se omenja nekaj klicev na 15 minut, kar ni uradna številka. Ta ritem ostane daleč pod tem.

Koledarsko leto in mesec se ob vsakem osveževanju izračunata iz časovnega pasu Home Assistanta. 1. januarja se osnova prestavi na novo leto. Za Slovenijo naj bo časovni pas `Europe/Ljubljana`.

HTTP 401 in 403 sprožita ponovno avtentikacijo. Časovna omejitev ali izpad povezave pustita senzorje nedosegljive, brez prekinitve Home Assistanta.

## Namestitev (HACS)

1. HACS → **Integracije** → tri pike zgoraj desno → **Shramba po meri**.
2. Repozitorij: `https://github.com/uzorcic/mojelektro`
3. Kategorija: **Integracija**.
4. V HACS poišči **Moj Elektro** in ga prenesi.
5. Znova zaženi Home Assistant.
6. **Nastavitve → Naprave in storitve → Dodaj integracijo → Moj Elektro**.

Zahtevana različica Home Assistanta je 2025.1 ali novejša (preizkušeno tudi z 2026.6).

## Namestitev (ročno)

Kopiraj mapo `custom_components/mojelektro` v `/config/custom_components/mojelektro`, tako da je `manifest.json` v tej mapi. Znova zaženi Home Assistant in dodaj integracijo, kot je opisano zgoraj.

## Nastavitev

1. V portalu [mojelektro.si](https://mojelektro.si/) se prijavi in ustvari API žeton (pogosto **Moj profil → API storitve / Kreiraj žeton**). Žeton se pokaže enkrat.
2. Žeton prilepi v polje **API žeton**. Integracija z njim pokliče `GET /reading-type`.
3. Merilno mesto:
   - če `GET /souporaba` vrne vloge, izberi EIMM s seznama ali **Vnesi ročno**,
   - sicer vpiši EIMM (na primer `3-123456`) ali GSRN.
4. EIMM se preveri z `GET /merilno-mesto/{identifikator}`. GSRN se preveri s kratkim klicem `GET /meter-readings`.

API nima metode, ki bi vrnila vsa merilna mesta na žetonu. Souporaba je edini seznam v specifikaciji in pri navadni samooskrbi pogosto ostane prazen. EIMM je takrat treba prepisati s kartice merilnega mesta.

## Značka na nadzorni plošči

Značke v tej obliki so na voljo od Home Assistant 2024.8 (pogledi tipa `sections`). Zamenjaj ID, če tvoj EIMM ni `3-123456`.

Uredi nadzorno ploščo → tri pike → urejevalnik YAML. Pri pogledu dodaj `badges:`:

```yaml
title: Dom
path: dom
type: sections
badges:
  - type: entity
    entity: sensor.moj_elektro_3_123456_letna_bilanca
    name: Letna bilanca
    icon: mdi:scale-balance
    show_name: true
    show_state: true
    show_icon: true
sections: []
```

Isto značko lahko dodaš tudi v urejevalniku nadzorne plošče, brez YAML-a: **Dodaj značko** in izberi `sensor.moj_elektro_…_letna_bilanca`.

Ko je bilanca negativna, senzor sam zamenja ikono v `mdi:solar-power` (višek oddaje). Ko je pozitivna, pokaže `mdi:home-import-outline`.

## Razvoj

```bash
pip install -r requirements-test.txt
pytest
```

Testi tečejo z `pytest-homeassistant-custom-component` in z JSON odgovori v obliki shem iz OpenAPI (`MeterReadings`, `ReadingType`, `Napaka` / `errors`, `MerilnoMesto`, `Souporaba`). Pravi žetoni v repozitorij ne sodijo.
