# {{NAME}}: bullet time for a Godot game (Rocsy REA recipe).
# This file is added to the game's pack as res://rocsy/{{CLASS}}.gd. It does nothing until the game
# loads it: the agent registers it as an autoload (project settings in the recovered project) or
# instances it from a script the game already runs. T toggles Engine.time_scale.
extends Node

const SLOW := 0.3
const BLEND_SECONDS := 0.25
var active := false
var current := 1.0

func _ready() -> void:
	process_mode = Node.PROCESS_MODE_ALWAYS
	print("[{{NAME}}] loaded: press T for bullet time")

func _unhandled_input(event: InputEvent) -> void:
	if event is InputEventKey and event.pressed and not event.echo and event.keycode == KEY_T:
		active = not active
		print("[{{NAME}}] bullet time ", "on" if active else "off")

func _process(_delta: float) -> void:
	if get_tree().paused:
		return
	var target := SLOW if active else 1.0
	var real_delta := get_process_delta_time() / max(Engine.time_scale, 0.001)
	current = move_toward(current, target, real_delta / BLEND_SECONDS)
	Engine.time_scale = current
