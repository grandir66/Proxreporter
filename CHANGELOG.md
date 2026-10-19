# Changelog

## 2026-10-19 — 2.22.0, installazione remota in un comando

- `configure` legge gli accessi da `accessi.json` lasciato dal comando unico di
  DA-Link, senza domande, e lo cancella. Perché: al collaudo del 19/10 sulla VM 900 il
  tecnico doveva incollare tre blocchi in ordine e leggere a mano l'impronta di ogni host.
- Fiducia al primo collegamento (decisione di Riccardo, 19/10): la prima chiave vista si
  salva, una chiave cambiata si rifiuta; `dimentica` la fa riprendere dopo una reinstallazione.
- `run` scrive `esito.json` con un codice per host (password rifiutata, chiave cambiata,
  host irraggiungibile…) e chiama `dopo-raccolta`: DA-Link mostra lo stato vero invece di
  «configurato». Le prove usano un server SSH vero su 127.0.0.1.

## 2026-10-19 — esecutore remoto

- Base Linux con raccolte SSH da più host senza installare l’agente su ogni nodo.
  Credenziali acquisite localmente, chiavi host verificate, configurazioni private
  e cifratura facoltativa. Guasti isolati per host e giro serializzato.
- Il percorso remoto esplicito prevale sul rilevamento locale; le notifiche dei
  nodi restano invariate. Il CLI e il profilo locale precedenti sono conservati.
- Survey separata e collaudo su host reali restano aperti, descritti nel README.
