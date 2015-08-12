#!/usr/bin/env python
# encoding: utf-8

"""
Editor view.

Provides means to view the generated script and edit it.
Also contains the dangerous run button that triggers the final run.
Once clicked, a counter will be shown, showing the total number
of killed files in terms of size.
"""


# Stdlib:
import os
import time
import logging

# External:
from gi.repository import Gtk
from gi.repository import GLib
from gi.repository import Pango
from gi.repository import Polkit
from gi.repository import GObject


# Internal:
from shredder.util import View, IconButton, scrolled, size_to_human_readable
from shredder.util import MultipleChoiceButton, SuggestedButton
from shredder.tree import Column
from shredder.runner import Script


LOGGER = logging.getLogger('editor')


REMOVED_LABEL = '''<big>{s}</big><small> removed</small>
<small>{t}</small> <b><big>{p}</big></b>
'''


#############################
# Try to load GtkSourceView #
#############################

try:
    from gi.repository import GtkSource

    LOGGER.info('Using GtkSourceView since we have it.')

    def _create_source_view():
        """Create a suitable text view + buffer for showing a sh script."""
        buffer_ = GtkSource.Buffer()
        buffer_.set_highlight_syntax(True)

        view = GtkSource.View()
        view.set_buffer(buffer_)
        view.set_show_line_numbers(True)
        view.set_show_line_marks(True)
        view.set_auto_indent(True)

        return view, buffer_

    def _set_source_style(view, style_name):
        """If supported, set a color scheme by name."""
        style = GtkSource.StyleSchemeManager.get_default().get_scheme(
            style_name
        )

        if style:
            buffer_ = view.get_buffer()
            buffer_.set_style_scheme(style)

    def _set_source_lang(view, lang):
        """If supported, set a syntax highlighter to use."""
        language = GtkSource.LanguageManager.get_default().get_language(lang)
        buffer_ = view.get_buffer()
        buffer_.set_language(language)

# Fallback to the normal Gtk.TextView if no GtkSource.View could be imported
except ImportError:
    LOGGER.info('No GtkSourceView found.')

    def _create_source_view():
        """Create a suitable text view + buffer for showing a sh script."""
        buffer_ = Gtk.Buffer()
        view = Gtk.TextView()
        return view, buffer_

    def _set_source_style(*_):
        """If supported, set a color scheme by name."""
        pass  # Not supported.

    def _set_source_lang(*_):
        """If supported, set a syntax highlighter to use."""
        pass  # Not supported.


def _create_running_screen():
    """Helper to configure a spinner for the delete screen."""
    spinner = Gtk.Spinner()
    spinner.start()
    return spinner


def _create_finished_screen(callback):
    """Give the user a nice, warm feeling."""
    control_grid = Gtk.Grid()
    control_grid.set_hexpand(False)
    control_grid.set_vexpand(False)
    control_grid.set_halign(Gtk.Align.CENTER)
    control_grid.set_valign(Gtk.Align.CENTER)

    label = Gtk.Label(
        use_markup=True,
        label='''<span font="65">✔</span>


<big><b>All went well!</b></big>




''',
        justify=Gtk.Justification.CENTER
    )
    label.get_style_context().add_class('dim-label')

    go_back = IconButton('go-jump-symbolic', 'Go back to Script')
    go_back.set_halign(Gtk.Align.CENTER)
    go_back.connect(
        'clicked', lambda _: callback()
    )

    control_grid.attach(label, 0, 0, 1, 1)
    control_grid.attach_next_to(
        go_back, label, Gtk.PositionType.BOTTOM, 1, 1
    )

    return control_grid


class RunningLabel(Gtk.Label):
    """Centered large label showing a size sum and the current deleted path."""
    def __init__(self):
        Gtk.Label.__init__(self)

        # Basename is more important:
        self.set_ellipsize(Pango.EllipsizeMode.START)

        # Make it appeared a bit dimmed:
        self.get_style_context().add_class(
            Gtk.STYLE_CLASS_DIM_LABEL
        )
        self.set_justify(Gtk.Justification.CENTER)

        self._size_sum = 0
        self.push(None, '', '')

    def push(self, model, prefix, path):
        """Push a new path to the label, removing the old one."""
        if prefix.lower() == 'keeping':
            return

        text = REMOVED_LABEL.format(
            t=prefix,
            s=size_to_human_readable(self._size_sum),
            p=GLib.markup_escape_text(path)
        )
        self.set_markup(text)

        if model is None:
            return

        node = model.lookup_by_path(path)
        if node is not None:
            self._size_sum += node[Column.SIZE]


class RunButton(Gtk.Box):
    """Customized run button that can change color."""
    dry_run = GObject.Property(type=bool, default=True)

    def __init__(self, icon, label):
        Gtk.Box.__init__(self)
        self.get_style_context().add_class(
            Gtk.STYLE_CLASS_LINKED
        )

        self.button = IconButton(icon, label)
        self.state = Gtk.ToggleButton()
        self.state.add(
            Gtk.Label(use_markup=True, label='<small>Dry run?</small>')
        )

        self.state.connect('toggled', self._toggle_dry_run)

        self.pack_start(self.button, True, True, 0)
        self.pack_start(self.state, False, False, 0)
        self.bind_property(
            'dry_run', self.state, 'active',
            GObject.BindingFlags.BIDIRECTIONAL |
            GObject.BindingFlags.SYNC_CREATE
        )

        self.state.set_active(True)
        self._toggle_dry_run(self.state)

    def _toggle_dry_run(self, btn):
        """Change the color and severeness of the button."""
        for widget in [self.button, self.state]:
            ctx = widget.get_style_context()
            if not btn.get_active():
                ctx.remove_class(Gtk.STYLE_CLASS_SUGGESTED_ACTION)
                ctx.add_class(Gtk.STYLE_CLASS_DESTRUCTIVE_ACTION)
            else:
                ctx.remove_class(Gtk.STYLE_CLASS_DESTRUCTIVE_ACTION)
                ctx.add_class(Gtk.STYLE_CLASS_SUGGESTED_ACTION)

    def set_sensitive(self, is_sensitive):
        """Overwrite Gtk.Widget.set_sensitive to disable style classes."""
        Gtk.Box.set_sensitive(self, is_sensitive)

        if not is_sensitive:
            ctx = self.state.get_style_context()
            ctx.remove_class(Gtk.STYLE_CLASS_SUGGESTED_ACTION)
            ctx.remove_class(Gtk.STYLE_CLASS_DESTRUCTIVE_ACTION)
        else:
            self._toggle_dry_run(self.state)


def _create_icon_stack():
    """Create a small widget that shows alternating icons."""
    icon_stack = Gtk.Stack()
    icon_stack.set_transition_type(
        Gtk.StackTransitionType.SLIDE_LEFT_RIGHT
    )

    for name, symbol in (('warning', '⚠'), ('danger', '☠')):
        icon_label = Gtk.Label(
            use_markup=True,
            justify=Gtk.Justification.CENTER
        )
        icon_label.get_style_context().add_class(
            Gtk.STYLE_CLASS_DIM_LABEL
        )
        icon_label.set_markup(
            '<span font="65">{symbol}</span>'.format(symbol=symbol)
        )
        icon_stack.add_named(icon_label, name)

    return icon_stack


class ScriptSaverDialog(Gtk.FileChooserWidget):
    """GtkFileChooserWidget tailored for saving a `Script` instance."""

    __gsignals__ = {
        'saved': (GObject.SIGNAL_RUN_FIRST, None, ()),
    }

    def __init__(self, editor_view):
        Gtk.FileChooserWidget.__init__(self)

        self.editor_view = editor_view
        self.set_select_multiple(False)
        self.set_create_folders(False)
        self.set_action(Gtk.FileChooserAction.SAVE)

        self.file_type = MultipleChoiceButton(
            ['sh', 'json', 'csv'],
            'sh',
            'sh',
            'Filetype to choose'
        )
        self.file_type.set_halign(Gtk.Align.START)
        self.file_type.set_hexpand(True)
        self.file_type.connect('row-selected', self.on_file_type_changed)

        self.confirm = SuggestedButton('Save')
        self.confirm.connect('clicked', self.on_save_clicked)
        self.confirm.set_halign(Gtk.Align.END)
        self.confirm.set_hexpand(True)
        self.confirm.set_sensitive(False)

        self.connect('selection-changed', self.on_selection_changed)

        extra_box = Gtk.Grid()
        extra_box.attach(self.file_type, 0, 0, 1, 1)
        extra_box.attach(self.confirm, 1, 0, 1, 1)
        extra_box.attach_next_to(
            Gtk.Label('Filetype: '),
            self.file_type,
            Gtk.PositionType.LEFT,
            1,
            1
        )
        extra_box.set_hexpand(True)
        extra_box.set_halign(Gtk.Align.FILL)
        self.set_extra_widget(extra_box)

        self.update_file_suggestion()

    def update_file_suggestion(self):
        """Suggest a name for the script to save."""
        file_type = self.file_type.get_selected_choice() or 'sh'
        self.set_current_name(time.strftime('rmlint-%FT%T%z.' + file_type))

    def on_file_type_changed(self, _):
        """Called once the user chose a different format"""
        current_path = self.get_filename()
        if not current_path:
            self.update_file_suggestion()
        else:
            try:
                path, ext = current_path.rsplit('.', 1)
                self.set_current_name(
                    path + '.' + self.file_type.get_selected_choice() or ''
                )
            except ValueError:
                # No extension. Leave it.
                pass

    def on_save_clicked(self, _):
        """Called once the user clicked the `Save` button"""
        file_type = self.file_type.get_selected_choice()
        abs_path = self.get_filename()

        LOGGER.info('Saving script to: %s', abs_path)
        self.editor_view.get_script().save(abs_path, file_type)
        self.emit('saved')

    def on_selection_changed(self, _):
        """Called once a file or directory was clicked"""
        self.confirm.set_sensitive(bool(self.get_filename()))


class OverlaySaveButton(Gtk.Overlay):
    """Button box that contains two buttons in a overlay.
    The overlay is shown on top of the script editor.
    Buttons are: A unlock button for asking for root permissions
    and a save button to save the script somewhere.
    """

    __gsignals__ = {
        'save-clicked': (GObject.SIGNAL_RUN_FIRST, None, ()),
        'unlock-clicked': (GObject.SIGNAL_RUN_FIRST, None, ())
    }

    def __init__(self):
        Gtk.Overlay.__init__(self)

        self._box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self._box.get_style_context().add_class(
            Gtk.STYLE_CLASS_LINKED
        )

        perm = Polkit.Permission.new_sync(
            'org.freedesktop.accounts.user-administration',
            Polkit.UnixProcess.new_for_owner(os.getpid(), 0, -1),
            None
        )
        self._lock_button = Gtk.LockButton()
        self._lock_button.set_permission(perm)
        self._lock_button.props.margin = 20
        self._lock_button.props.margin_end = 0
        self._lock_button.connect(
            'clicked', lambda _: self.emit('unlock-clicked')
        )

        self._save_button = IconButton(
            'folder-download-symbolic', 'Save to file'
        )
        self._save_button.props.margin = 20
        self._save_button.props.margin_start = 0
        self._save_button.connect(
            'clicked', lambda _: self.emit('save-clicked')
        )

        self._box.pack_start(self._lock_button, False, True, 0)
        self._box.pack_start(self._save_button, False, True, 0)
        self._box.set_hexpand(False)
        self._box.set_vexpand(False)
        self._box.set_halign(Gtk.Align.END)
        self._box.set_valign(Gtk.Align.END)
        self.add_overlay(self._box)


class EditorView(View):
    """Actual view class."""
    def __init__(self, win):
        View.__init__(self, win)

        self._runner = None
        self.script = Script.create_dummy()

        control_grid = Gtk.Grid()
        control_grid.set_hexpand(False)
        control_grid.set_vexpand(False)
        control_grid.set_halign(Gtk.Align.CENTER)
        control_grid.set_valign(Gtk.Align.CENTER)

        label = Gtk.Label(
            use_markup=True,
            justify=Gtk.Justification.CENTER
        )
        label.get_style_context().add_class(
            Gtk.STYLE_CLASS_DIM_LABEL
        )
        label.set_markup('''

<big><b>Review the script on the left!</b></big>
When done, click the `Run Script` button below.
\n\n''')

        icon_stack = _create_icon_stack()

        self.text_view, buffer_ = _create_source_view()
        self.text_view.set_name('ShredderScriptEditor')
        self.text_view.set_vexpand(True)
        self.text_view.set_valign(Gtk.Align.FILL)
        self.text_view.set_hexpand(True)
        self.text_view.set_halign(Gtk.Align.FILL)
        self.save_button = OverlaySaveButton()
        self.save_button.add(scrolled(self.text_view))
        self.save_chooser = ScriptSaverDialog(self)
        self.save_button.connect(
            'save-clicked',
            lambda _: self.left_stack.set_visible_child_name('chooser')
        )
        self.save_chooser.connect(
            'saved',
            lambda _: self.left_stack.set_visible_child_name('script')
        )

        buffer_.create_tag("original", weight=Pango.Weight.BOLD)
        buffer_.create_tag("normal")

        self.run_label = RunningLabel()
        self.run_label.set_hexpand(False)
        self.run_label.set_halign(Gtk.Align.FILL)

        self.left_stack = Gtk.Stack()
        self.left_stack.set_transition_type(
            Gtk.StackTransitionType.OVER_RIGHT_LEFT
        )

        self.left_stack.add_named(self.save_button, 'script')
        self.left_stack.add_named(self.save_chooser, 'chooser')
        self.left_stack.add_named(scrolled(self.run_label), 'list')

        separator = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        left_pane = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        left_pane.pack_start(self.left_stack, True, True, 0)
        left_pane.pack_start(separator, False, False, 0)

        self.run_button = RunButton(
            'user-trash-symbolic', 'Run Script'
        )
        self.run_button.button.connect('clicked', self.on_run_script_clicked)
        self.run_button.set_halign(Gtk.Align.CENTER)
        self.run_button.connect(
            'notify::dry-run',
            lambda btn, _: icon_stack.set_visible_child_name(
                'warning' if btn.dry_run else 'danger'
            )
        )

        control_grid.attach(label, 0, 0, 1, 1)
        control_grid.attach_next_to(
            self.run_button, label, Gtk.PositionType.BOTTOM, 1, 1
        )
        control_grid.attach_next_to(
            icon_stack, label, Gtk.PositionType.TOP, 1, 1
        )
        control_grid.set_border_width(15)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_UP)

        self.stack.add_named(control_grid, 'danger')
        self.stack.add_named(_create_running_screen(), 'progressing')

        self.stack.add_named(
            _create_finished_screen(self._switch_back), 'finished'
        )

        self.left_stack.set_visible_child_name('script')

        grid = Gtk.Grid()
        grid.attach(left_pane, 0, 0, 1, 1)
        grid.attach_next_to(
            self.stack, left_pane, Gtk.PositionType.RIGHT, 1, 1
        )
        self.add(grid)

    def _switch_back(self):
        """Switch back from delete-view to script view"""
        self.run_button.set_sensitive(False)
        self.switch_to_script()

    def switch_to_script(self):
        """Read and show the script."""
        self.sub_title = 'Check the results'
        self.left_stack.set_visible_child_name('script')
        buffer_ = self.text_view.get_buffer()
        buffer_.set_text(self.script.read())

        # Make sure it gets colored again:
        _set_source_style(self.text_view, 'solarized-light')
        _set_source_lang(self.text_view, 'sh')
        self.stack.set_visible_child_name('danger')

    def on_run_script_clicked(self, _):
        """The critical function callback that is run when action is done."""
        self.sub_title = 'Shreddering. Cross fingers!'
        self.stack.set_visible_child_name('progressing')
        self.left_stack.set_visible_child_name('list')

        model = self.app_window.views['runner'].model

        self.script.connect(
            'line-read',
            lambda _, prefix, line: self.run_label.push(model, prefix, line)
        )

        self.script.connect(
            'script-finished',
            lambda *_: self.stack.set_visible_child_name('finished')
        )
        self.script.run(dry_run=True)

    def on_view_enter(self):
        """Called once the view becomes visible."""
        self.run_button.set_sensitive(True)

        # Re-read the script.
        run_script = self.app_window.views['runner'].script
        if run_script:
            self.script = run_script
        self.switch_to_script()

    def get_script(self):
        """Get the current Script instance.
        This will be always valid (i.e. not None)
        """
        return self.script