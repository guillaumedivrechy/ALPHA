# -*- coding: utf-8 -*-
"""Faux coupleur Wago 750-362 : serveur Modbus TCP minimal, sans dépendance.

Sert à tester le VRAI code de communication (io_banc/wago.py, client
pymodbus) sans matériel : mapping des adresses, entrées inversées, mise à
l'échelle, réponses d'erreur, latence, coupure puis retour du coupleur.

Fonctions servies : 1 (coils), 2 (entrées TOR), 3 (holding registers),
4 (input registers), 5 (écriture coil), 6 (écriture registre)."""

from __future__ import annotations

import socket
import struct
import threading
import time


class FauxCoupleur:
    def __init__(self, port: int = 0):
        self.ir: dict[int, int] = {}          # input registers (AI, RTD)
        self.di: dict[int, bool] = {}         # entrées TOR
        self.hr: dict[int, int] = {}          # sorties analogiques écrites
        self.co: dict[int, bool] = {}         # sorties TOR écrites
        self.ecritures: list[tuple] = []      # (fonction, adresse, valeur)
        self.latence_s = 0.0                  # délai avant chaque réponse
        self.erreurs_restantes = 0            # réponses d'exception à servir
        self.transactions = 0
        self._verrou = threading.Lock()
        self._clients: list[socket.socket] = []
        self._ecoute: socket.socket | None = None
        self._en_service = False
        self.port = port
        self.demarrer()

    # --------------------------------------------------------- cycle de vie
    def demarrer(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", self.port))
        self.port = s.getsockname()[1]
        s.listen(4)
        self._ecoute = s
        self._en_service = True
        threading.Thread(target=self._accepter, args=(s,), daemon=True).start()

    def arreter(self) -> None:
        """Coupleur hors tension / câble débranché : écoute et connexions
        fermées. Sous Linux, close() ne débloque pas un accept() en cours :
        shutdown() d'abord, et toute connexion acceptée hors service est
        refermée aussitôt."""
        self._en_service = False
        if self._ecoute is not None:
            for action in (lambda: self._ecoute.shutdown(socket.SHUT_RDWR),
                           self._ecoute.close):
                try:
                    action()
                except OSError:
                    pass
            self._ecoute = None
        with self._verrou:
            for c in self._clients:
                try:
                    c.shutdown(socket.SHUT_RDWR)
                    c.close()
                except OSError:
                    pass
            self._clients.clear()

    # ----------------------------------------------------------- protocole
    def _accepter(self, ecoute: socket.socket) -> None:
        while True:
            try:
                c, _ = ecoute.accept()
            except OSError:
                return
            if not self._en_service:
                c.close()
                return
            with self._verrou:
                self._clients.append(c)
            threading.Thread(target=self._servir, args=(c,), daemon=True).start()

    @staticmethod
    def _lire(c: socket.socket, n: int) -> bytes:
        donnees = b""
        while len(donnees) < n:
            bloc = c.recv(n - len(donnees))
            if not bloc:
                raise ConnectionError
            donnees += bloc
        return donnees

    def _servir(self, c: socket.socket) -> None:
        try:
            while True:
                tid, pid, longueur, unite = struct.unpack(">HHHB", self._lire(c, 7))
                pdu = self._lire(c, longueur - 1)
                if not self._en_service:
                    raise ConnectionError
                if self.latence_s:
                    time.sleep(self.latence_s)
                reponse = self._traiter(pdu)
                c.sendall(struct.pack(">HHHB", tid, pid, len(reponse) + 1, unite)
                          + reponse)
        except (ConnectionError, OSError, struct.error):
            pass
        finally:
            try:
                c.close()
            except OSError:
                pass

    def _traiter(self, pdu: bytes) -> bytes:
        fc = pdu[0]
        with self._verrou:
            self.transactions += 1
            if self.erreurs_restantes > 0:
                self.erreurs_restantes -= 1
                return bytes([fc | 0x80, 0x04])          # défaillance esclave
            adr, val = struct.unpack(">HH", pdu[1:5])
            if fc in (3, 4):
                table = self.hr if fc == 3 else self.ir
                mots = [table.get(adr + i, 0) & 0xFFFF for i in range(val)]
                return bytes([fc, 2 * val]) + struct.pack(f">{val}H", *mots)
            if fc in (1, 2):
                table = self.co if fc == 1 else self.di
                bits = [bool(table.get(adr + i, False)) for i in range(val)]
                octets = bytearray((val + 7) // 8)
                for i, b in enumerate(bits):
                    if b:
                        octets[i // 8] |= 1 << (i % 8)
                return bytes([fc, len(octets)]) + bytes(octets)
            if fc == 5:
                self.co[adr] = val == 0xFF00
                self.ecritures.append((5, adr, self.co[adr]))
                return pdu[:5]
            if fc == 6:
                self.hr[adr] = val
                self.ecritures.append((6, adr, val))
                return pdu[:5]
            return bytes([fc | 0x80, 0x01])              # fonction illégale
