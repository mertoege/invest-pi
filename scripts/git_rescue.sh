#!/usr/bin/env bash
#
# git_rescue.sh — rettet fremde, noch nicht gepushte Commits ueber ein
# `git reset --hard origin/main` hinweg.
#
# ZUM SOURCEN, nicht zum Ausfuehren. Erwartet: cwd == Repo, eine log()-Funktion.
#
# WARUM ES DAS GIBT (Datenverlust 01.08.2026):
# Zwei Timer schreiben alle 2 Minuten im selben Arbeitsverzeichnis — auto_pull.sh
# und status_push.sh. Beide haben denselben Notausgang, wenn ein `pull --rebase`
# scheitert: `git reset --hard origin/main`. Der raeumt aber nicht nur den eigenen
# Schlamassel weg, sondern JEDEN lokalen Commit, der es noch nicht auf origin
# geschafft hat.
# Am 01.08.2026 um 16:38:45 committete der Portfolio-Manager Merts UNH-Kauf in die
# config.yaml. 58 Sekunden spaeter lief status_push in genau diesen Konflikt (weil
# auto_pull zeitgleich im selben Repo rebaste) und setzte hart auf origin/main
# zurueck. Gerettet wurde nur der eigene Status-Snapshot. Der Kauf war weg — ohne
# Fehlermeldung. Merts Depot-Anzeige stimmte danach zwei Wochen lang nicht: sie
# zeigte einen VWCE-Kauf, den es nie gab, und den echten UNH-Kauf gar nicht.
#
# WAS ES TUT: vor dem Reset alle lokalen Commits beiseitelegen, die NICHT vom
# Automatismus selbst stammen (also alles ausser "status: ..."), und danach per
# cherry-pick zurueckspielen. Klappt das nicht, bleibt ein Rettungs-Tag stehen und
# es gibt eine laute Log-Zeile — verloren ist dann nichts, es braucht nur eine Hand.

FOREIGN_COMMITS=""

# Vor jedem `reset --hard origin/main` aufrufen. Setzt $FOREIGN_COMMITS.
rescue_foreign_commits() {
    FOREIGN_COMMITS=""
    # --reverse: aelteste zuerst, damit das spaetere cherry-pick die Reihenfolge haelt.
    FOREIGN_COMMITS=$(git log --reverse --format='%H %s' origin/main..HEAD 2>/dev/null \
                      | awk '$2 != "status:" {print $1}')
    if [ -n "$FOREIGN_COMMITS" ]; then
        local anzahl
        anzahl=$(echo "$FOREIGN_COMMITS" | wc -l)
        log "reset wuerde $anzahl fremde(n) Commit(s) verwerfen — werden gesichert:"
        git log --format='  %h %s' origin/main..HEAD 2>/dev/null \
            | grep -v ' status: ' | tee -a "$LOG" >&2
        # Tag als Netz, falls der cherry-pick unten scheitert oder das Skript stirbt.
        local sha
        for sha in $FOREIGN_COMMITS; do
            git tag -f "rettung/$(echo "$sha" | cut -c1-8)" "$sha" >/dev/null 2>&1 || true
        done
    fi
}

# Direkt nach dem `reset --hard origin/main` aufrufen.
restore_foreign_commits() {
    [ -n "$FOREIGN_COMMITS" ] || return 0
    local sha kurz
    for sha in $FOREIGN_COMMITS; do
        kurz=$(echo "$sha" | cut -c1-8)
        if git cherry-pick --allow-empty --keep-redundant-commits "$sha" >>"$LOG" 2>&1; then
            log "fremder Commit $kurz zurueckgespielt"
            git tag -d "rettung/$kurz" >/dev/null 2>&1 || true
        else
            git cherry-pick --abort >/dev/null 2>&1 || true
            log "ACHTUNG: fremder Commit $kurz liess sich nicht zurueckspielen — liegt als Tag rettung/$kurz bereit, bitte von Hand pruefen"
        fi
    done
    FOREIGN_COMMITS=""
}
