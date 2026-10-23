# {{NAME}}: a Godot mod script (Rocsy REA), added to the game's pack as res://rocsy/{{CLASS}}.gd.
# Put changed copies of the game's own files under src/files/ with their res:// paths
# (e.g. src/files/scenes/player.gd replaces res://scenes/player.gd). The agent wires this in.
extends Node

func _ready() -> void:
	print("[{{NAME}}] loaded")
