extends Node
## Makes one region react. Add a plain Node as a child of each region in your
## map, attach this script, and fill in the fields in the Inspector.
##
## Out of the box it tints the parent (any 2D node or Control) by mood.
## Replace or extend _react() to drive your own art: swap sprites, play
## animations, spawn guards, change music, and so on.
## Requires the RegionDirector autoload.

@export var region_id := ""                 ## unique id, e.g. "north". Defaults to the parent's name.
@export var display_name := ""              ## shown in chat. Defaults to the id.
@export_multiline var personality := ""     ## e.g. "Proud mountain clans, slow to trust, quick to anger."
@export_range(0.0, 1.0) var start_hostility := 0.2
@export_range(0.0, 1.0) var start_prosperity := 0.5

const MOOD_TINTS := {
	"calm": Color(1.0, 1.0, 1.0),
	"curious": Color(0.8, 0.95, 1.0),
	"wary": Color(1.0, 0.95, 0.7),
	"fearful": Color(0.75, 0.75, 0.95),
	"angry": Color(1.0, 0.6, 0.55),
	"jubilant": Color(0.8, 1.0, 0.75),
}

var _tween: Tween


func _ready() -> void:
	if region_id == "":
		region_id = str(get_parent().name).to_snake_case()
	if display_name == "":
		display_name = region_id.capitalize()

	RegionDirector.region_changed.connect(_on_region_changed)
	RegionDirector.register_region(region_id, display_name, personality, {
		"hostility": start_hostility,
		"prosperity": start_prosperity,
	})


func _on_region_changed(id: String, state: Dictionary) -> void:
	if id == region_id:
		_react(state)


## state has: mood (String), hostility (0..1), prosperity (0..1).
func _react(state: Dictionary) -> void:
	var target := get_parent() as CanvasItem
	if target == null:
		return  # 3D or non-visual parent: put your own reaction here.

	var tint: Color = MOOD_TINTS.get(state["mood"], Color.WHITE)
	# Struggling regions look duller.
	tint = tint.darkened((1.0 - float(state["prosperity"])) * 0.35)

	if _tween:
		_tween.kill()
	_tween = create_tween()
	_tween.tween_property(target, "modulate", tint, 0.6)
