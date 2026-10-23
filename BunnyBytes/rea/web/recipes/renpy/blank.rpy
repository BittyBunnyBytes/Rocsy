# {{NAME}}: a Ren'Py mod (Rocsy REA). Ren'Py loads every .rpy in game/; the zz_ prefix makes this
# load after the game's own scripts so it can override them. Filled in by the Master Agent.
init 999 python:
    renpy.log("{{NAME}} loaded")
