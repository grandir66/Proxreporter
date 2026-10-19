# Changelog

## 2026-10-19 — esecutore remoto

- Base Linux con raccolte SSH da più host senza installare l’agente su ogni nodo.
  Credenziali acquisite localmente, chiavi host verificate, configurazioni private
  e cifratura facoltativa. Guasti isolati per host e giro serializzato.
- Il percorso remoto esplicito prevale sul rilevamento locale; le notifiche dei
  nodi restano invariate. Il CLI e il profilo locale precedenti sono conservati.
- Survey separata e collaudo su host reali restano aperti, descritti nel README.
