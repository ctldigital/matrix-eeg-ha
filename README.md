# MATRIX EEG für Home Assistant

Verbindet eine Wärmepumpe oder einen Boiler in Home Assistant mit der MATRIX-EEG-Plattform. Die Plattform
hebt bei Überschuss den Warmwasser-Sollwert an (Boost), die Integration setzt ihn danach wieder zurück.
Die Integration hat einen eigenen MQTT-Client und braucht die MQTT-Integration von Home Assistant nicht.

## Installation

1. HACS > ⋮ > Benutzerdefinierte Repositories: `https://github.com/ctldigital/matrix-eeg-ha` als Typ „Integration“ hinzufügen.
2. „MATRIX EEG“ installieren und Home Assistant neu starten.
3. Einstellungen > Geräte & Dienste > Integration hinzufügen > „MATRIX EEG“.

## Einrichtung

- **Broker-URL**: Standard `mqtts://mqtt.matrix.ctl.digital:8883`. Erlaubt sind `mqtts://`, `mqtt://` (nur für Tests
  oder einen lokalen Broker) und `wss://…/mqtt`. Das Zertifikat wird gegen die System-CAs geprüft.
- **Benutzername und Passwort** aus dem Geräte-Assistenten der Plattform.
- **Entitäten**:
  - Warmwasser-Sollwert (Pflicht): `water_heater` oder `number`.
  - Warmwasser-Ist-Temperatur (Pflicht): `sensor`, oder ein `water_heater` (Attribut `current_temperature`).
  - Leistung (optional): `sensor` in W oder kW.
  - Heizstab läuft (optional): `binary_sensor`, `switch` oder `sensor` (ein = `on` oder ein Wert über 0).
  - Max. Warmwassertemperatur (optional): `sensor` oder `number`. Der Sollwert wird nie darüber gesetzt.
  - Obergrenze: 30 bis 60 °C, Standard 60 °C. Die Integration setzt nie einen höheren Sollwert.

Alles lässt sich später unter „Konfigurieren“ ändern. Ein Eintrag ist ein Gerät auf der Plattform.

## Verhalten

- Ein Boost gilt nur für eine begrenzte Zeit (Lease). Kommt kein neuer Befehl, setzt die Integration den
  Normal-Sollwert zurück, auch nach einem Neustart von Home Assistant.
- Ändert jemand den Sollwert während eines Boosts, setzt die Integration ihn nie zurück.
- Der MQTT-Vertrag (Topics `eeg/<benutzername>/state`, `cmd`, `ack`, `status`) steht in `contract/v1/README.md`.

## Entwicklung

`uv run pytest`
