"""
    Flowblade Movie Editor is a nonlinear video editor.
    Copyright 2012 Janne Liljeblad.

    This file is part of Flowblade Movie Editor <http://code.google.com/p/flowblade>.

    Flowblade Movie Editor is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    Flowblade Movie Editor is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with Flowblade Movie Editor.  If not, see <http://www.gnu.org/licenses/>.
"""

"""
Module handles clip effects editing logic and gui
"""
import cairo
import copy
from gi.repository import GLib
from gi.repository import Gtk, Gdk
import pickle
import threading
import time

import appconsts
import atomicfile
import dialogs
import dialogutils
import edit
import editorpersistance
import editorstate
from editorstate import PROJECT
import gui
import guicomponents
import guiutils
import mltfilters
import propertyedit
import propertyeditorbuilder
import respaths
import tlinerender
import translations
import updater
import utils

_filter_stack = None
    
widgets = utils.EmptyClass()

#clip = None # Clip being edited
#track = None # Track of the clip being editeds
#clip_index = None # Index of clip being edited
_block_changed_update = False # Used to block unwanted callback update from "changed"
_block_stack_update = False # Used to block full stack update when adding new filter. 
                            # Otherwise we got 2 updates EditAction objects must always try to update
                            # on undo/redo.
#current_filter_index = -1 # Needed to find right filter object when saving/loading effect values

# Property change polling.
# We didn't put a layer of indirection to look for and launch events on filter property edits
# so now we detect filter edits by polling. This has no performance impect, n is so small.
_edit_polling_thread = None
filter_changed_since_last_save = False

# This is updated when filter panel is displayed and cleared when removed.
# Used to update kfeditors with external tline frame position changes
keyframe_editor_widgets = []

# Filter stack DND requires some state info to be maintained to make sure that it's only done when certain events
# happen in a certain sequence.
NOT_ON = 0
MOUSE_PRESS_DONE = 1
INSERT_DONE = 2
stack_dnd_state = NOT_ON
stack_dnd_event_time = 0.0
stack_dnd_event_info = None

filters_notebook_index = 2 # 2 for single window, app.py sets to 1 for two windows

# ---------------------------------------------------------- filter stack objects
class FilterFooterRow:
    
    def __init__(self, filter_object, filter_stack):
        self.filter_object = filter_object
        self.filter_stack = filter_stack
        
        surface = cairo.ImageSurface.create_from_png(respaths.IMAGE_PATH + "filter_save.png")
        save_button = guicomponents.PressLaunch(self.save_pressed, surface, w=22, h=22)
        save_button.widget.set_tooltip_markup(_("Save effect values"))
        
        surface = cairo.ImageSurface.create_from_png(respaths.IMAGE_PATH + "filter_load.png")
        load_button = guicomponents.PressLaunch(self.load_pressed, surface, w=22, h=22)
        load_button.widget.set_tooltip_markup(_("Load effect values"))

        surface = cairo.ImageSurface.create_from_png(respaths.IMAGE_PATH + "filter_reset.png")
        reset_button = guicomponents.PressLaunch(self.reset_pressed, surface, w=22, h=22)
        reset_button.widget.set_tooltip_markup(_("Reset effect values"))
        
        surface = guiutils.get_cairo_image("filters_mask_add")
        mask_button = guicomponents.PressLaunch(self.add_mask_pressed, surface, w=22, h=22)
        mask_button.widget.set_tooltip_markup(_("Add Filter Mask"))
        
        self.widget = Gtk.HBox(False, 0)
        self.widget.pack_start(guiutils.pad_label(4,5), False, False, 0)
        self.widget.pack_start(mask_button.widget, False, False, 0)
        self.widget.pack_start(guiutils.pad_label(2,5), False, False, 0)
        self.widget.pack_start(reset_button.widget, False, False, 0)
        self.widget.pack_start(guiutils.pad_label(12,5), False, False, 0)
        self.widget.pack_start(save_button.widget, False, False, 0)
        self.widget.pack_start(load_button.widget, False, False, 0)

        self.widget.pack_start(Gtk.Label(), True, True, 0)
        
    def save_pressed(self, w, e):
        default_name = self.filter_object.info.name + _("_effect_values") + ".data"
        dialogs.save_effects_compositors_values(_save_effect_values_dialog_callback, default_name, True, self.filter_object)

    def load_pressed(self, w, e):
        dialogs.load_effects_compositors_values_dialog(_load_effect_values_dialog_callback, True, self.filter_object)
        
    def reset_pressed(self, w, e):
        _reset_filter_values(self.filter_object)

    def add_mask_pressed(self, w, e):
        filter_index = self.filter_stack.get_filter_index(self.filter_object)
        _filter_mask_launch_pressed(w, e, filter_index)

class FilterHeaderRow:
    
    def __init__(self, filter_object):
        self.filter_name_label = Gtk.Label(label= "<b>" + filter_object.info.name + "</b>")
        self.filter_name_label.set_use_markup(True)
        self.icon = Gtk.Image.new_from_pixbuf(filter_object.info.get_icon())

        hbox = Gtk.HBox(False, 0)
        hbox.pack_start(guiutils.pad_label(4,5), False, False, 0)
        hbox.pack_start(self.icon, False, False, 0)
        hbox.pack_start(self.filter_name_label, False, False, 0)
        hbox.pack_start(Gtk.Label(), True, True, 0)

        self.widget = hbox


class FilterStackItem:

    def __init__(self, filter_object, edit_panel, filter_stack):
        self.filter_object = filter_object
        self.filter_header_row = FilterHeaderRow(filter_object)

        self.edit_panel = edit_panel
        self.edit_panel_frame = Gtk.Frame()
        self.edit_panel_frame.add(edit_panel)
        self.edit_panel_frame.set_shadow_type(Gtk.ShadowType.NONE)
        
        self.filter_stack = filter_stack

        self.expander = Gtk.Expander()
        self.expander.set_label_widget(self.filter_header_row.widget)
        self.expander.add(self.edit_panel_frame)
        self.expander.set_resize_toplevel(True)
        self.expander.set_label_fill(True)

        self.expander_frame = Gtk.Frame()
        self.expander_frame.add(self.expander)
        self.expander_frame.set_shadow_type(Gtk.ShadowType.NONE)
        guiutils.set_margins(self.expander_frame, 2, 0, 0, 0)
        
        self.active_check = Gtk.CheckButton()
        self.active_check.set_active(True)
        self.active_check.connect("toggled", self.toggle_filter_active)
        guiutils.set_margins(self.active_check, 4, 0, 0, 0)

        self.active_check_vbox = Gtk.VBox(False, 0)
        self.active_check_vbox.pack_start(self.active_check, False, False, 0)
        self.active_check_vbox.pack_start(Gtk.Label(), True, True, 0)

        surface = cairo.ImageSurface.create_from_png(respaths.IMAGE_PATH + "trash.png")
        trash_button = guicomponents.PressLaunch(self.trash_pressed, surface, w=22, h=22)
        
        self.trash_vbox = Gtk.VBox(False, 0)
        self.trash_vbox.pack_start(trash_button.widget, False, False, 0)
        self.trash_vbox.pack_start(Gtk.Label(), True, True, 0)
        
        self.widget = Gtk.HBox(False, 0)
        self.widget.pack_start(self.active_check_vbox, False, False, 0)
        self.widget.pack_start(self.expander_frame, True, True, 0)
        self.widget.pack_start(self.trash_vbox, False, False, 0)
        self.widget.show_all()

    def trash_pressed(self, w, e):
        self.filter_stack.delete_filter_for_stack_item(self)
    
    def toggle_filter_active(self, widget):
        self.filter_object.active = (self.filter_object.active == False)
        self.filter_object.update_mlt_disabled_value()


class ClipFilterStack:

    def __init__(self, clip, track, clip_index):
        self.clip = clip
        self.track = track
        self.clip_index = clip_index
        
        # Create filter stack and GUI
        self.filter_stack = []
        self.widget = Gtk.VBox(False, 0)
        for filter_index in range(0, len(clip.filters)):
            filter_object = clip.filters[filter_index]
            edit_panel = _get_filter_panel(clip, filter_object, filter_index, track, clip_index)
            footer_row = FilterFooterRow(filter_object, self)
            edit_panel.pack_start(footer_row.widget, False, False, 0)
            edit_panel.pack_start(guiutils.pad_label(12,12), False, False, 0)
            stack_item = FilterStackItem(filter_object, edit_panel, self)
            self.filter_stack.append(stack_item)
            self.widget.pack_start(stack_item.widget,False, False, 0)
        
        self.widget.show_all()

    def get_filters(self):
        filters = []
        for stack_item in self.filter_stack:
            filters.append(stack_item.filter_object)
        return filters

    def reinit_stack_item(self, filter_object):
        stack_index = -1
        for i in range(0, len(self.filter_stack)):
            stack_item = self.filter_stack[i]
            if stack_item.filter_object is filter_object:
                stack_index = i 
        
        if stack_index != -1:
            # Remove panels from box
            children = self.widget.get_children()
            for child in children:
                self.widget.remove(child)
                
            # Remove old stack item for reseted filter.
            self.filter_stack.pop(stack_index)
            
            # Create new stack item
            edit_panel = _get_filter_panel(self.clip, filter_object, stack_index, self.track, self.clip_index )
            footer_row = FilterFooterRow(filter_object, self)
            edit_panel.pack_start(footer_row.widget, False, False, 0)
            edit_panel.pack_start(guiutils.pad_label(12,12), False, False, 0)
            stack_item = FilterStackItem(filter_object, edit_panel, self)
            
            # Put eveything back
            self.filter_stack.insert(stack_index, stack_item)
            for stack_item in self.filter_stack:
                self.widget.pack_start(stack_item.widget,False, False, 0)
                
            self.set_filter_item_expanded(stack_index)
            
    def get_clip_data(self):
        return (self.clip, self.track, self.clip_index)
    
    def get_filter_index(self, filter_object):
        #filter_index = -1
        #for i in range(0, ) self.filter_stack 
        return self.clip.filters.index(filter_object)
        
    def set_filter_item_expanded(self, filter_index):
        filter_stack_item = self.filter_stack[filter_index]
        filter_stack_item.expander.set_expanded(True)

    def delete_filter_for_stack_item(self, stack_item):
        filter_index = self.filter_stack.index(stack_item)
        delete_effect_pressed(self.clip, filter_index)

    def stack_changed(self, clip):
        if len(clip.filters) != len(self.filter_stack):
            return True

        for i in range(0, len(clip.filters)):
            clip_filter_info = clip.filters[i].info
            stack_filter_info = self.filter_stack[i].info
            
            if stack_filter_info.mlt_service_id != clip_filter_info.mlt_service_id:
                return True

        return False

# ------------------------------------------------------------------- interface
def shutdown_polling():
    global _edit_polling_thread
    if _edit_polling_thread != None:
        _edit_polling_thread.shutdown()
        _edit_polling_thread = None

def clip_is_being_edited(clip):
    if _filter_stack == None:
        return False
    
    if _filter_stack.clip == clip:
        return True
        
    return False

def get_edited_clip():
    if _filter_stack == None:
        return None
    else:
        return  _filter_stack.clip

def get_clip_effects_editor_panel():
    create_widgets()

    info_row = Gtk.HBox(False, 2)
    info_row.pack_start(widgets.hamburger_launcher.widget, False, False, 0)
    info_row.pack_start(Gtk.Label(), True, True, 0)
    info_row.pack_start(widgets.clip_info, False, False, 0)
    info_row.pack_start(Gtk.Label(), True, True, 0)

    return info_row

def _group_selection_changed(group_combo, filters_list_view):
    group_name, filters_array = mltfilters.groups[group_combo.get_active()]
    filters_list_view.fill_data_model(filters_array)
    filters_list_view.treeview.get_selection().select_path("0")

def set_clip(clip, track, clip_index, show_tab=True):
    """
    Sets clip being edited and inits gui.
    """
    print("set_clip")

    if _filter_stack != None:
        if clip == _filter_stack.clip and track == _filter_stack.track and clip_index == _filter_stack.clip_index and show_tab == False:
            print("return")
            return
    
    widgets.clip_info.display_clip_info(clip, track, clip_index)
    set_enabled(True)
    update_stack(clip, track, clip_index)

    if len(clip.filters) > 0:
        pass # remove if nothing needed here.
    else:
        show_text_in_edit_area(_("Clip Has No Filters"))

    if show_tab:
        gui.middle_notebook.set_current_page(filters_notebook_index)

    global _edit_polling_thread
    # Close old polling
    if _edit_polling_thread != None:
        _edit_polling_thread.shutdown()
    # Start new polling
    _edit_polling_thread = PropertyChangePollingThread()
    _edit_polling_thread.start()

def set_filter_item_expanded(filter_index):
    if _filter_stack == None:
        return 
    
    _filter_stack.set_filter_item_expanded(filter_index)

def effect_select_row_double_clicked(treeview, tree_path, col):
    add_currently_selected_effect()

def filter_stack_button_press(treeview, event):
    path_pos_tuple = treeview.get_path_at_pos(int(event.x), int(event.y))
    if path_pos_tuple == None:
        row = -1 # Empty row was clicked
    else:
        path, column, x, y = path_pos_tuple
        selection = treeview.get_selection()
        selection.unselect_all()
        selection.select_path(path)
        (model, rows) = selection.get_selected_rows()
        row = max(rows[0])
    if row == -1:
        return False
    if event.button == 3:
        guicomponents.display_filter_stack_popup_menu(row, treeview, _filter_stack_menu_item_selected, event)                                    
        return True
    return False

def _filter_stack_menu_item_selected(widget, data):
    item_id, row, treeview = data

    if item_id == "toggle":
        toggle_filter_active(row)
    if item_id == "reset":
        reset_filter_values()
    if item_id == "movedown":
        delete_row = row
        insert_row = row + 2
        if insert_row > len(clip.filters):
            insert_row = len(clip.filters)
        do_stack_move(insert_row, delete_row)
    if item_id == "moveup":
        delete_row = row + 1
        insert_row = row - 1
        if insert_row < 0:
            insert_row = 0
        do_stack_move(insert_row, delete_row)
        
def _quit_editing_clip_clicked(): # this is a button callback
    clear_clip()

def clear_clip():
    """
    Removes clip from effects editing gui.
    """
    global _filter_stack
    _filter_stack = None
    _set_no_clip_info()
    #effect_selection_changed()
    show_text_in_edit_area(_("No Clip"))

    set_enabled(False)
    shutdown_polling()

def _set_no_clip_info():
    widgets.clip_info.set_no_clip_info()

def create_widgets():
    """
    Widgets for editing clip effects properties.
    """
    # Aug-2019 - SvdB - BB
    prefs = editorpersistance.prefs

    widgets.clip_info = guicomponents.ClipInfoPanel()
    
    widgets.value_edit_box = Gtk.VBox()
    widgets.value_edit_frame = Gtk.Frame()
    widgets.value_edit_frame.set_shadow_type(Gtk.ShadowType.NONE)
    widgets.value_edit_frame.add(widgets.value_edit_box)

    widgets.toggle_all = Gtk.Button()
    widgets.toggle_all.set_image(guiutils.get_image("filters_all_toggle"))
    
    filter_mask_surfaces = [guiutils.get_cairo_image("filters_mask_add"), guiutils.get_cairo_image("filters_mask_add_not_active")]
    widgets.add_filter_mask = guicomponents.HamburgerPressLaunch(_filter_mask_launch_pressed, filter_mask_surfaces, 26)
    guiutils.set_margins(widgets.add_filter_mask.widget, 10, 0, 1, 0)

    widgets.toggle_all.connect("clicked", lambda w: toggle_all_pressed())

    widgets.hamburger_launcher = guicomponents.HamburgerPressLaunch(_hamburger_launch_pressed)
    guiutils.set_margins(widgets.hamburger_launcher.widget, 6, 8, 1, 0)

    widgets.toggle_all.set_tooltip_text(_("Toggle all Filters On/Off"))
    widgets.add_filter_mask.widget.set_tooltip_text(_("Add Filter Mask"))
    
    widgets.panel_header = Gtk.HBox(False, 0)
    widgets.panel_header.pack_start(widgets.toggle_all, False, False, 0)
    widgets.panel_header.pack_start(widgets.add_filter_mask.widget, False, False, 0)
 
def set_enabled(value):
    widgets.clip_info.set_enabled( value)
    widgets.toggle_all.set_sensitive(value)
    widgets.hamburger_launcher.set_sensitive(value)
    widgets.hamburger_launcher.widget.queue_draw()
    widgets.add_filter_mask.set_sensitive(value)
    widgets.hamburger_launcher.widget.queue_draw()

def set_stack_update_blocked():
    global _block_stack_update
    _block_stack_update = True

def set_stack_update_unblocked():
    global _block_stack_update
    _block_stack_update = False

def update_stack(clip, track, clip_index):
    new_stack = ClipFilterStack(clip, track, clip_index)
    global _filter_stack
    _filter_stack = new_stack

    global widgets
    widgets.value_edit_frame.remove(widgets.value_edit_box)
    widgets.value_edit_frame.add(_filter_stack.widget)

    widgets.value_edit_box = _filter_stack.widget

def update_stack_changed_blocked():
    global _block_changed_update
    _block_changed_update = True
    update_stack()
    _block_changed_update = False
    
def add_currently_selected_effect():
    # Check we have clip
    if clip == None:
        return
    
    filter_info = get_selected_filter_info()
    action = get_filter_add_action(filter_info, clip)
    action.do_edit() # gui update in callback from EditAction object.
    
    updater.repaint_tline()

def get_filter_add_action(filter_info, target_clip):
    # Maybe show info on using alpha filters
    if filter_info.group == "Alpha":
        GLib.idle_add(_alpha_filter_add_maybe_info, filter_info)

    data = {"clip":target_clip, 
            "filter_info":filter_info,
            "filter_edit_done_func":filter_edit_done_stack_update}
    action = edit.add_filter_action(data)

    return action

def _alpha_filter_add_maybe_info(filter_info):
    if editorpersistance.prefs.show_alpha_info_message == True and \
       editorstate. current_sequence().compositing_mode != appconsts.COMPOSITING_MODE_STANDARD_FULL_TRACK:
        dialogs.alpha_info_msg(_alpha_info_dialog_cb, translations.get_filter_name(filter_info.name))

def _alpha_info_dialog_cb(dialog, response_id, dont_show_check):
    if dont_show_check.get_active() == True:
        editorpersistance.prefs.show_alpha_info_message = False
        editorpersistance.save()

    dialog.destroy()

def get_selected_filter_info():
    # Get current selection on effects treeview - that's a vertical list.
    treeselection = gui.effect_select_list_view.treeview.get_selection()
    (model, rows) = treeselection.get_selected_rows()    
    row = rows[0]
    row_index = max(row)
    
    # Add filter
    group_name, filters_array = mltfilters.groups[gui.effect_select_combo_box.get_active()]
    return filters_array[row_index]
    
def add_effect_pressed():
    add_currently_selected_effect()

def delete_effect_pressed(clip, filter_index):
    set_stack_update_blocked()

    current_filter = clip.filters[filter_index]
    
    if current_filter.info.filter_mask_filter == "":
        # Regular filters
        data = {"clip":clip,
                "index":filter_index,
                "filter_edit_done_func":filter_edit_done_stack_update}
        action = edit.remove_filter_action(data)
        action.do_edit()
    else:
        # Filter mask filters.
        index_1 = -1
        index_2 = -1
        for i in range(0, len(clip.filters)):
            f = clip.filters[i]
            if f.info.filter_mask_filter != "":
                if index_1 == -1:
                    index_1 = i
                else:
                    index_2 = i
        
        data = {"clip":clip,
                "index_1":index_1,
                "index_2":index_2,
                "filter_edit_done_func":filter_edit_done_stack_update}
        action = edit.remove_two_filters_action(data)
        action.do_edit()

    set_stack_update_unblocked()

    clip, track, clip_index = _filter_stack.get_clip_data()
    set_clip(clip, track, clip_index)

    updater.repaint_tline()
    
def toggle_all_pressed():
    for i in range(0, len(clip.filters)):
        filter_object = clip.filters[i]
        filter_object.active = (filter_object.active == False)
        filter_object.update_mlt_disabled_value()
    
    update_stack()
"""    
def dnd_row_deleted(model, path):
    now = time.time()
    global stack_dnd_state, stack_dnd_event_time, stack_dnd_event_info
    if stack_dnd_state == INSERT_DONE:
        if (now - stack_dnd_event_time) < 0.1:
            stack_dnd_state = NOT_ON
            insert_row = int(stack_dnd_event_info)
            delete_row = int(path.to_string())
            stack_dnd_event_info = (insert_row, delete_row)
            # Because of dnd is gtk thing for some internal reason it needs to complete before we go on
            # touching storemodel again with .clear() or it dies in gtktreeviewaccessible.c
            GLib.idle_add(do_dnd_stack_move)
        else:
            stack_dnd_state = NOT_ON
    else:
        stack_dnd_state = NOT_ON
        
def dnd_row_inserted(model, path, tree_iter):
    global stack_dnd_state, stack_dnd_event_time, stack_dnd_event_info
    if stack_dnd_state == MOUSE_PRESS_DONE:
        stack_dnd_state = INSERT_DONE
        stack_dnd_event_time = time.time()
        stack_dnd_event_info = path.to_string()
    else:
        stack_dnd_state = NOT_ON

def do_dnd_stack_move():
    insert, delete_row = stack_dnd_event_info
    do_stack_move(insert, delete_row)
    
def do_stack_move(insert_row, delete_row):
    if abs(insert_row - delete_row) < 2: # filter was dropped on its previous place or cannot moved further up or down
        return
    
    # The insert insert_row and delete_row values are rows we get when listening 
    # "row-deleted" and "row-inserted" events after setting treeview "reorderable"
    # Dnd is detected by order and timing of these events together with mouse press event
    data = {"clip":clip,
            "insert_index":insert_row,
            "delete_index":delete_row,
            "filter_edit_done_func":filter_edit_done_stack_update}
    action = edit.move_filter_action(data)
    action.do_edit()
            
def stack_view_pressed():
    global stack_dnd_state
    stack_dnd_state = MOUSE_PRESS_DONE
"""
""" This was called from edit gui update to fix some bug I think, look if/how we need this going forward
def reinit_current_effect():
    print("reinit_current_effect")
    clip, track, clip_index = _filter_stack.get_clip_data()
    set_clip(clip, track, clip_index)
"""

def reinit_stack_if_needed(force_update):
    clip, track, clip_index = _filter_stack.get_clip_data()
    if _filter_stack.stack_changed(clip) == True or force_update == True:
        print("reinit_stack_if_needed calls set clip")
        set_clip(clip, track, clip_index, show_tab=True)

def effect_selection_changed(use_current_filter_index=False):
    global keyframe_editor_widgets, current_filter_index

    # Check we have clip
    if clip == None:
        keyframe_editor_widgets = []
        show_text_in_edit_area(_("No Clip"))
        return
    
    # Check we actually have filters so we can display one.
    # If not, clear previous filters from view.
    if len(clip.filters) == 0:
        show_text_in_edit_area(_("Clip Has No Filters"))
        keyframe_editor_widgets = []
        return
    
    # "changed" get's called twice when adding filter and selecting last
    # so we use this do this only once 
    if _block_changed_update == True:
        return

    # We need this update on clip load into editor
    if _clip_has_filter_mask_filter() == True:
        widgets.add_filter_mask.set_sensitive(False)
    else:
        widgets.add_filter_mask.set_sensitive(True)
        
    keyframe_editor_widgets = []

    # Get selected row which is also index of filter in clip.filters
    treeselection = widgets.effect_stack_view.treeview.get_selection()
    (model, rows) = treeselection.get_selected_rows()

    # If we don't get legal selection select first filter
    try:
        row = rows[0]
        filter_index = max(row)
    except:
        filter_index = 0

    # use_current_filter_index == False is used when user changes edited filter or clip.
    if use_current_filter_index == True:
        filter_index = current_filter_index

    filter_object = clip.filters[filter_index]


def _get_filter_panel(clip, filter_object, filter_index, track, clip_index):
 
    # current_filter_index = filter_index
    
    # Create EditableProperty wrappers for properties
    editable_properties = propertyedit.get_filter_editable_properties(
                                                               clip, 
                                                               filter_object,
                                                               filter_index,
                                                               track,
                                                               clip_index)

    # Get editors and set them displayed
    vbox = Gtk.VBox(False, 0)
    try:
        filter_name = translations.filter_names[filter_object.info.name]
    except KeyError:
        filter_name = filter_object.info.name

    #filter_name_label = Gtk.Label(label= "<b>" + filter_name + "</b>")
    #filter_name_label.set_use_markup(True)
    

    #vbox.pack_start(filter_header_row.widget, False, False, 0)
    vbox.pack_start(guicomponents.EditorSeparator().widget, False, False, 0)

    if len(editable_properties) > 0:
        # Create editor row for each editable property
        for ep in editable_properties:
            editor_row = propertyeditorbuilder.get_editor_row(ep)
            if editor_row == None:
                continue

            # Set keyframe editor widget to be updated for frame changes if such is created 
            try:
                editor_type = ep.args[propertyeditorbuilder.EDITOR]
            except KeyError:
                editor_type = propertyeditorbuilder.SLIDER # this is the default value
            
            if ((editor_type == propertyeditorbuilder.KEYFRAME_EDITOR)
                or (editor_type == propertyeditorbuilder.KEYFRAME_EDITOR_RELEASE)
                or (editor_type == propertyeditorbuilder.KEYFRAME_EDITOR_CLIP)
                or (editor_type == propertyeditorbuilder.FILTER_RECT_GEOM_EDITOR)
                or (editor_type == propertyeditorbuilder.KEYFRAME_EDITOR_CLIP_FADE_FILTER)):
                    keyframe_editor_widgets.append(editor_row)
            
            # if slider property is being edited as keyrame property
            if hasattr(editor_row, "is_kf_editor"):
                keyframe_editor_widgets.append(editor_row)

            vbox.pack_start(editor_row, False, False, 0)
            if not hasattr(editor_row, "no_separator"):
                vbox.pack_start(guicomponents.EditorSeparator().widget, False, False, 0)
            
        # Create NonMltEditableProperty wrappers for properties
        non_mlteditable_properties = propertyedit.get_non_mlt_editable_properties( clip, 
                                                                                   filter_object,
                                                                                   filter_index)

        # Extra editors. Editable properties may have already been created 
        # with "editor=no_editor" and now extra editors may be created to edit those
        # Non mlt properties are added as these are only needed with extraeditors
        editable_properties.extend(non_mlteditable_properties)
        editor_rows = propertyeditorbuilder.get_filter_extra_editor_rows(filter_object, editable_properties)
        for editor_row in editor_rows:
            vbox.pack_start(editor_row, False, False, 0)
            if not hasattr(editor_row, "no_separator"):
                vbox.pack_start(guicomponents.EditorSeparator().widget, False, False, 0)
    else:
        vbox.pack_start(Gtk.Label(label=_("No editable parameters")), True, True, 0)
    vbox.show_all()

    return vbox


def show_text_in_edit_area(text):
    vbox = Gtk.VBox(False, 0)

    filler = Gtk.EventBox()
    filler.add(Gtk.Label())
    vbox.pack_start(filler, True, True, 0)
    
    info = Gtk.Label(label=text)
    info.set_sensitive(False)
    filler = Gtk.EventBox()
    filler.add(info)
    vbox.pack_start(filler, False, False, 0)
    
    filler = Gtk.EventBox()
    filler.add(Gtk.Label())
    vbox.pack_start(filler, True, True, 0)

    vbox.show_all()

    scroll_window = Gtk.ScrolledWindow()
    scroll_window.add_with_viewport(vbox)
    scroll_window.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    scroll_window.show_all()

    widgets.value_edit_frame.remove(widgets.value_edit_box)
    widgets.value_edit_frame.add(scroll_window)

    widgets.value_edit_box = scroll_window

def clear_effects_edit_panel():
    widgets.value_edit_frame.remove(widgets.value_edit_box)
    label = Gtk.Label()
    widgets.value_edit_frame.add(label)
    widgets.value_edit_box = label

def filter_edit_done_stack_update(edited_clip, index=-1):
    """
    EditAction object calls this after edits and undos and redos.
    Methods updates filter stack to new state. 
    """
    print("filter_edit_done_stack_update")
    if _block_stack_update == True:
        print("blocked")
        return
        
    if edited_clip != get_edited_clip(): # This gets called by all undos/redos, we only want to update if clip being edited here is affected
        return
    

    
    global _block_changed_update
    _block_changed_update = True
    update_stack()
    _block_changed_update = False

    if _clip_has_filter_mask_filter() == True:
        widgets.add_filter_mask.set_sensitive(False)
    else:
        widgets.add_filter_mask.set_sensitive(True)

    # Select row in effect stack view and to display corresponding effect editor panel.
    if not(index < 0):
        widgets.effect_stack_view.treeview.get_selection().select_path(str(index))
    else: # no effects after edit, clear effect editor panel
        clear_effects_edit_panel()

def display_kfeditors_tline_frame(frame):
    for kf_widget in keyframe_editor_widgets:
        kf_widget.display_tline_frame(frame)

def update_kfeditors_sliders(frame):
    for kf_widget in keyframe_editor_widgets:
        kf_widget.update_slider_value_display(frame)
        
def update_kfeditors_positions():
    if _filter_stack == None:
        return 

    for kf_widget in keyframe_editor_widgets:
        kf_widget.update_clip_pos()


# ------------------------------------------------ FILTER MASK 
def _filter_mask_launch_pressed(widget, event, filter_index):
    filter_names, filter_msgs = mltfilters.get_filter_mask_start_filters_data()
    guicomponents.get_filter_mask_menu(event, _filter_mask_item_activated, filter_names, filter_msgs, filter_index)

def _filter_mask_item_activated(widget, data):
    if _filter_stack == None:
        return False
    
    clip, track, clip_index = _filter_stack.get_clip_data()
    full_stack_mask, msg, current_filter_index = data
    
    filter_info_1 = mltfilters.get_filter_mask_filter(msg)
    filter_info_2 = mltfilters.get_filter_mask_filter("Mask - End")

    if full_stack_mask == True:
        index_1 = 0
        index_2 = len(clip.filters) + 1
    else:
        if current_filter_index != -1:
            index_1 = current_filter_index
            index_2 = current_filter_index + 2
        else:
            index_1 = 0
            index_2 = len(clip.filters) + 1

    data = {"clip":clip, 
            "filter_info_1":filter_info_1,
            "filter_info_2":filter_info_2,
            "index_1":index_1,
            "index_2":index_2,
            "filter_edit_done_func":filter_edit_done_stack_update}
    action = edit.add_two_filters_action(data)

    set_stack_update_blocked()
    action.do_edit()
    set_stack_update_unblocked()

    set_clip(clip, track, clip_index)
    _filter_stack.set_filter_item_expanded(current_filter_index + 1)

def _clip_has_filter_mask_filter():
    if clip == None:
        return False
    
    for f in clip.filters:
        if f.info.filter_mask_filter != "":
            return True
          
    return False

# ------------------------------------------------ SAVE, LOAD etc. from hamburger menu
def _hamburger_launch_pressed(widget, event):
    guicomponents.get_clip_effects_editor_hamburger_menu(event, _clip_hamburger_item_activated)
    
def _clip_hamburger_item_activated(widget, msg):
    if msg == "fade_length":
        dialogs.set_fade_length_default_dialog(_set_fade_length_dialog_callback, PROJECT().get_project_property(appconsts.P_PROP_DEFAULT_FADE_LENGTH))
    elif msg == "close":
        clear_clip()
        
def _save_effect_values_dialog_callback(dialog, response_id, filter_object):
    if response_id == Gtk.ResponseType.ACCEPT:
        save_path = dialog.get_filenames()[0]
        effect_data = EffectValuesSaveData(filter_object)
        effect_data.save(save_path)
    
    dialog.destroy()

def _load_effect_values_dialog_callback(dialog, response_id, filter_object):
    if response_id == Gtk.ResponseType.ACCEPT:
        load_path = dialog.get_filenames()[0]
        effect_data = utils.unpickle(load_path)
        
        if effect_data.data_applicable(filter_object.info):
            effect_data.set_effect_values(filter_object)
            _filter_stack.reinit_stack_item(filter_object)
        else:
            # Info window
            saved_effect_name = effect_data.info.name
            current_effect_name = filter_object.info.name
            primary_txt = _("Saved Filter data not applicaple for this Filter!")
            secondary_txt = _("Saved data is for ") + saved_effect_name + " Filter,\n" + _("current edited Filter is ") + current_effect_name + "."
            dialogutils.warning_message(primary_txt, secondary_txt, gui.editor_window.window)
    
    dialog.destroy()

def _reset_filter_values(filter_object):
        filter_object.properties = copy.deepcopy(filter_object.info.properties)
        filter_object.non_mlt_properties = copy.deepcopy(filter_object.info.non_mlt_properties)
        filter_object.update_mlt_filter_properties_all()
                
        _filter_stack.reinit_stack_item(filter_object)

def _set_fade_length_dialog_callback(dialog, response_id, spin):
    if response_id == Gtk.ResponseType.ACCEPT:
        default_length = int(spin.get_value())
        PROJECT().set_project_property(appconsts.P_PROP_DEFAULT_FADE_LENGTH, default_length)
        
    dialog.destroy()
    
class PropertyChangePollingThread(threading.Thread):
    
    def __init__(self):
        threading.Thread.__init__(self)
        self.last_properties = None
        
    def run(self):

        self.running = True
        while self.running:
            
            if _filter_stack == None:
                self.shutdown()
            else:
                if self.last_properties == None:
                    self.last_properties = self.get_clip_filters_properties()
                
                new_properties = self.get_clip_filters_properties()
                
                changed = False
                for new_filt_props, old_filt_props in zip(new_properties, self.last_properties):
                        for new_prop, old_prop in zip(new_filt_props, old_filt_props):
                            if new_prop != old_prop:
                                changed = True

                if changed:
                    global filter_changed_since_last_save
                    filter_changed_since_last_save = True
                    tlinerender.get_renderer().timeline_changed()

                self.last_properties = new_properties
                
                time.sleep(1.0)

    def get_clip_filters_properties(self):
        filters_properties = []
        for filt in _filter_stack.get_filters():
            filt_props = []
            for prop in filt.properties:
                filt_props.append(copy.deepcopy(prop))

            filters_properties.append(filt_props)
        
        return filters_properties
        
    def shutdown(self):
        self.running = False


class EffectValuesSaveData:
    
    def __init__(self, filter_object):
        self.info = filter_object.info
        self.multipart_filter = self.info.multipart_filter

        # Values of these are edited by the user.
        self.properties = copy.deepcopy(filter_object.properties)
        try:
            self.non_mlt_properties = copy.deepcopy(filter_object.non_mlt_properties)
        except:
            self.non_mlt_properties = [] # Versions prior 0.14 do not have non_mlt_properties and fail here on load

        if self.multipart_filter == True:
            self.value = filter_object.value
        else:
            self.value = None
        
    def save(self, save_path):
        with atomicfile.AtomicFileWriter(save_path, "wb") as afw:
            write_file = afw.get_file()
            pickle.dump(self, write_file)
        
    def data_applicable(self, filter_info):
        if isinstance(self.info, filter_info.__class__):
            return self.info.__dict__ == filter_info.__dict__
        return False

    def set_effect_values(self, filter_object):
        if self.multipart_filter == True:
            filter_object.value = self.value
         
        filter_object.properties = copy.deepcopy(self.properties)
        filter_object.non_mlt_properties = copy.deepcopy(self.non_mlt_properties)
        filter_object.update_mlt_filter_properties_all()
         
    
