extends PanelContainer
## Chat box for talking to the world. Builds its own UI, so there is no scene
## to wire up.
##
## Setup: add a CanvasLayer to your main scene, add a PanelContainer under it,
## attach this script, and anchor it where you want it (e.g. bottom left).
## Requires the RegionDirector autoload.
##
## Enter focuses the box during gameplay; Escape hands the keyboard back.

@export var box_size := Vector2(440, 260)

var _history: RichTextLabel
var _entry: LineEdit


func _ready() -> void:
	custom_minimum_size = box_size

	var column := VBoxContainer.new()
	add_child(column)

	_history = RichTextLabel.new()
	_history.bbcode_enabled = true
	_history.scroll_following = true
	_history.size_flags_vertical = Control.SIZE_EXPAND_FILL
	column.add_child(_history)

	_entry = LineEdit.new()
	_entry.placeholder_text = "Say or do something..."
	_entry.max_length = 300
	column.add_child(_entry)

	_entry.text_submitted.connect(_on_submitted)
	RegionDirector.narration.connect(_on_narration)
	RegionDirector.region_reaction.connect(_on_region_reaction)
	RegionDirector.failed.connect(_on_failed)
	RegionDirector.busy_changed.connect(_on_busy_changed)


func _input(event: InputEvent) -> void:
	if event.is_action_pressed("ui_cancel") and _entry.has_focus():
		_entry.release_focus()
		get_viewport().set_input_as_handled()


func _unhandled_key_input(event: InputEvent) -> void:
	var key := event as InputEventKey
	if key and key.pressed and not key.echo and key.keycode in [KEY_ENTER, KEY_KP_ENTER]:
		if not _entry.has_focus():
			_entry.grab_focus()
			get_viewport().set_input_as_handled()


func _on_submitted(text: String) -> void:
	text = text.strip_edges()
	if text == "" or RegionDirector.is_busy():
		return
	_entry.clear()
	_history.append_text("[color=#9ecbff]You:[/color] %s\n" % _escape(text))
	RegionDirector.send_player_message(text)


func _on_narration(text: String) -> void:
	_history.append_text("[i]%s[/i]\n" % _escape(text))


func _on_region_reaction(region_id: String, text: String) -> void:
	var region_name := str(RegionDirector.regions[region_id]["name"])
	_history.append_text("[color=#ffd479]%s:[/color] %s\n" % [_escape(region_name), _escape(text)])


func _on_failed(message: String) -> void:
	_history.append_text("[color=#ff7b72]%s[/color]\n" % _escape(message))


func _on_busy_changed(is_busy: bool) -> void:
	_entry.editable = not is_busy
	_entry.placeholder_text = "The world is reacting..." if is_busy else "Say or do something..."
	if not is_busy:
		_entry.grab_focus()


func _escape(text: String) -> String:
	return text.replace("[", "[lb]")
