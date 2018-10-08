"""
Panes allow wrapping external objects and rendering them as part of
a dashboard.
"""
from __future__ import absolute_import

import os
import sys
import inspect
import base64
from io import BytesIO
from collections import OrderedDict

try:
    from html import escape
except:
    from cgi import escape

import param

from bokeh.layouts import Row as _BkRow, WidgetBox as _BkWidgetBox
from bokeh.models import LayoutDOM, CustomJS, Widget as _BkWidget, Div as _BkDiv

from .util import (Div, basestring, unicode, get_method_owner, push,
                   remove_root)
from .viewable import Reactive, Viewable


def Pane(obj, **kwargs):
    """
    Converts any object to a Pane if a matching Pane class exists.
    """
    if isinstance(obj, Viewable):
        return obj
    return PaneBase.get_pane_type(obj)(obj, **kwargs)


class PaneBase(Reactive):
    """
    PaneBase is the abstract baseclass for all atomic displayable units
    in the panel library. Pane defines an extensible interface for
    wrapping arbitrary objects and transforming them into Bokeh models.

    Panes are reactive in the sense that when the object they are
    wrapping is changed any dashboard containing the pane will update
    in response.

    To define a concrete Pane type subclass this class and implement
    the applies classmethod and the _get_model private method.
    """

    object = param.Parameter(default=None, doc="""
        The object being wrapped, which will be converted into a Bokeh model.""")

    # When multiple Panes apply to an object, the one with the highest
    # numerical precedence is selected. The default is an intermediate value.
    precedence = 0.5

    # Declares whether Pane supports updates to the Bokeh model
    _updates = False

    __abstract = True

    @classmethod
    def applies(cls, obj):
        """
        Given the object return a boolean indicating whether the Pane
        can render the object.
        """
        return None

    @classmethod
    def get_pane_type(cls, obj):
        if isinstance(obj, Viewable):
            return type(obj)
        descendents = [(p.precedence, p) for p in param.concrete_descendents(PaneBase).values()]
        pane_types = reversed(sorted(descendents, key=lambda x: x[0]))
        for _, pane_type in pane_types:
            if not pane_type.applies(obj): continue
            return pane_type
        raise TypeError('%s type could not be rendered.' % type(obj).__name__)

    def __init__(self, object, **params):
        if not self.applies(object):
            name = type(self).__name__
            raise ValueError('%s object not understood by %s, '
                             'expected %s object.' %
                             (type(object).__name__, name, name[:-5]))
        super(PaneBase, self).__init__(object=object, **params)

    def _get_root(self, doc, comm=None):
        root = _BkRow()
        model = self._get_model(doc, root, root, comm)
        root.children = [model]
        return root

    def _cleanup(self, model=None, final=False):
        super(PaneBase, self)._cleanup(model, final)
        if final:
            self.object = None

    def _update(self, model):
        """
        If _updates=True this method is used to update an existing Bokeh
        model instead of replacing the model entirely. The supplied model
        should be updated with the current state.
        """
        raise NotImplementedError

    def _link_object(self, model, doc, root, parent, comm=None):
        """
        Links the object parameter to the rendered Bokeh model, triggering
        an update when the object changes.
        """
        def update_pane(change, history=[model]):
            old_model = history[0]

            # Pane supports model updates
            if self._updates:
                def update_models():
                    self._update(old_model)
            else:
                # Otherwise replace the whole model
                self._cleanup(old_model)
                new_model = self._get_model(doc, root, parent, comm)
                def update_models():
                    index = parent.children.index(old_model)
                    parent.children[index] = new_model
                    history[0] = new_model

            if comm:
                update_models()
                push(doc, comm)
            else:
                doc.add_next_tick_callback(update_models)

        ref = model.ref['id']
        self._callbacks[ref].append(self.param.watch(update_pane, 'object'))


class Bokeh(PaneBase):
    """
    Bokeh panes allow including any Bokeh model in a panel.
    """

    precedence = 0.8

    @classmethod
    def applies(cls, obj):
        return isinstance(obj, LayoutDOM)

    def _get_model(self, doc, root=None, parent=None, comm=None):
        """
        Should return the Bokeh model to be rendered.
        """
        model = self.object
        if isinstance(model, _BkWidget):
            box_kws = {k: getattr(model, k) for k in ['width', 'height', 'sizing_mode']
                       if k in model.properties()}
            model = _BkWidgetBox(model, **box_kws)

        if root:
            plot_id = root.ref['id']
            if plot_id:
                for js in model.select({'type': CustomJS}):
                    js.code = js.code.replace(self.object.ref['id'], plot_id)

        if model._document and doc is not model._document:
            remove_root(model, doc)

        self._link_object(model, doc, root, parent, comm)
        return model


class HoloViews(PaneBase):
    """
    HoloViews panes render any HoloViews object to a corresponding
    Bokeh model while respecting the currently selected backend.
    """

    backend= param.String(default=None, doc="""
        The HoloViews backend used to render the plot (if None defaults
        to the currently selected renderer).""")

    show_widgets = param.Boolean(default=True, doc="""
        Whether to display widgets for the object. If disabled the
        widget_box may be laid out manually.""")

    widgets = param.Dict(default={}, doc="""
        A mapping from dimension name to a widget instance which will
        be used to override the default widgets.""")

    precedence = 0.8

    def __init__(self, object, **params):
        super(HoloViews, self).__init__(object, **params)
        from .layout import WidgetBox
        self.widget_box = WidgetBox()

    @classmethod
    def applies(cls, obj):
        if 'holoviews' not in sys.modules:
            return False
        from holoviews import Dimensioned
        return isinstance(obj, Dimensioned)

    def _patch_plot(self, plot, plot_id, comm):
        if not hasattr(plot, '_update_callbacks'):
            return

        for subplot in plot.traverse(lambda x: x):
            subplot.comm = comm
            for cb in getattr(subplot, 'callbacks', []):
                for c in cb.callbacks:
                    c.code = c.code.replace(plot.id, plot_id)

    def _cleanup(self, model=None, final=False):
        """
        Traverses HoloViews object to find and clean up any streams
        connected to existing plots.
        """
        from holoviews.core.spaces import DynamicMap, get_nested_streams
        dmaps = self.object.traverse(lambda x: x, [DynamicMap])
        for dmap in dmaps:
            for stream in get_nested_streams(dmap):
                for _, sub in stream._subscribers:
                    if inspect.ismethod(sub):
                        owner = get_method_owner(sub)
                        if owner.state is model:
                            owner.cleanup()
        super(HoloViews, self)._cleanup(model, final)

    def _render(self, doc, comm):
        from holoviews import Store, renderer
        if not Store.renderers:
            loaded_backend = (self.backend or 'bokeh')
            renderer(loaded_backend)
            Store.current_backend = loaded_backend
        backend = self.backend or Store.current_backend
        renderer = Store.renderers[backend]
        if backend == 'bokeh':
            renderer = renderer.instance(mode='server' if comm is None else 'default')
        kwargs = {'doc': doc} if backend == 'bokeh' else {}
        return renderer.get_plot(self.object, **kwargs)

    def _get_model(self, doc, root=None, parent=None, comm=None):
        """
        Should return the Bokeh model to be rendered.
        """
        plot = self._render(doc, comm)
        self._patch_plot(plot, root.ref['id'], comm)
        child_pane = Pane(plot.state, _temporary=True)
        model = child_pane._get_model(doc, root, parent, comm)
        widgets = self.widgets_from_dimensions(self.object, self.widgets)
        layout = model
        if widgets:
            self.widget_box.objects = widgets
            if self.show_widgets:
                layout = _BkRow()
                wbox = self.widget_box._get_model(doc, root, parent, comm)
                layout.children = [model, wbox]
            self._link_widgets(widgets, child_pane, layout, plot, comm)
        self._link_object(layout, doc, root, parent, comm)
        return layout

    def _link_widgets(self, widgets, pane, model, plot, comm):
        def update_plot(change):
            from holoviews.plotting.bokeh.plot import BokehPlot
            key = tuple(w.value for w in widgets)
            if isinstance(plot, BokehPlot):
                if comm:
                    plot.update(key)
                    plot.push()
                else:
                    def update_plot():
                        plot.update(key)
                    plot.document.add_next_tick_callback(update_plot)
            else:
                plot.update(key)
                pane.object = plot.state

        for w in widgets:
            watcher = w.param.watch(update_plot, 'value')
            self._callbacks[model.ref['id']].append(watcher)

    @classmethod
    def widgets_from_dimensions(cls, object, widget_types={}):
        from holoviews.core import Dimension
        from holoviews.core.util import isnumeric, unicode
        from holoviews.core.traversal import unique_dimkeys
        from .widgets import Widget, DiscreteSlider, Select, FloatSlider

        dims, keys = unique_dimkeys(object)
        if dims == [Dimension('Frame')] and keys == [(0,)]:
            return []

        values = dict(zip(dims, zip(*keys)))
        widgets = []
        for dim in dims:
            widget_type = None
            vals = dim.values or values.get(dim, None)
            if dim.name in widget_types:
                widget = widget_types[dim.name]
                if isinstance(widget, Widget):
                    widgets.append(widget)
                    continue
                elif isinstance(widget, type) and issubclass(widget, Widget):
                    widget_type = widget
                else:
                    raise ValueError('Explicit widget definitions expected '
                                     'to be a widget instance or type, %s '
                                     'dimension widget declared as %s.' %
                                     (dim, widget))
            if vals:
                if all(isnumeric(v) for v in vals):
                    vals = sorted(vals)
                    labels = [unicode(dim.pprint_value(v)) for v in vals]
                    options = OrderedDict(zip(labels, vals))
                    widget_type = widget_type or DiscreteSlider
                else:
                    options = list(vals)
                    widget_type = widget_type or Select
                default = vals[0] if dim.default is None else dim.default
                widget = widget_type(name=dim.label, options=options, value=default)
            elif dim.range != (None, None):
                default = dim.range[0] if dim.default is None else dim.default
                step = 0.1 if dim.step is None else dim.step
                widget_type = widget_type or FloatSlider
                widget = widget_type(step=step, name=dim.label, start=dim.range[0],
                                     end=dim.range[1], value=default)
            widgets.append(widget)
        return widgets



class DivPaneBase(PaneBase):
    """
    Baseclass for Panes which render HTML inside a Bokeh Div.
    See the documentation for Bokeh Div for more detail about
    the supported options like style and sizing_mode.
    """

    # DivPane supports updates to the model
    _updates = True

    __abstract = True

    height = param.Integer(default=None, bounds=(0, None))

    width = param.Integer(default=None, bounds=(0, None))

    sizing_mode = param.ObjectSelector(default=None, allow_None=True,
        objects=["fixed", "scale_width", "scale_height", "scale_both", "stretch_both"], 
        doc="How the item being displayed should size itself.")
                                       
    style = param.Dict(default=None, doc="""
        Dictionary of CSS property:value pairs to apply to this Div.""")

    def _get_properties(self):
        return {p : getattr(self,p) for p in ["width", "height", "sizing_mode", "style"]
                if getattr(self,p) is not None}

    def _get_model(self, doc, root=None, parent=None, comm=None):
        model = Div(**self._get_properties())
        self._link_object(model, doc, root, parent, comm)
        return model

    def _update(self, model):
        div = model if isinstance(model, _BkDiv) else model.children[0]
        div.update(**self._get_properties())


class Image(DivPaneBase):
    """
    Encodes an image as base64 and wraps it in a Bokeh Div model.  
    This is an abstract base class that needs the image type
    to be specified and specific code for determining the image shape.
    
    The imgtype determines the filetype, extension, and MIME type for
    this image. Each image type (png,jpg,gif) has a base class that
    supports anything with a `_repr_X_` method (where X is `png`,
    `gif`, etc.), a local file with the given file extension, or a
    HTTP(S) url with the given extension.  Subclasses of each type can
    provide their own way of obtaining or generating a PNG.
    """

    imgtype = 'None'

    @classmethod
    def applies(cls, obj):
        imgtype = cls.imgtype
        return (hasattr(obj, '_repr_'+imgtype+'_') or
                (isinstance(obj, basestring) and
                 ((os.path.isfile(obj) and obj.endswith('.'+imgtype)) or
                  ((obj.startswith('http://') or obj.startswith('https://'))
                   and obj.endswith('.'+imgtype)))))

    def _img(self):
        if not isinstance(self.object, basestring):
            return getattr(self.object, '_repr_'+self.imgtype+'_')()
        elif os.path.isfile(self.object):
            with open(self.object, 'rb') as f:
                return f.read()
        else:
            import requests
            r = requests.request(url=self.object, method='GET')
            return r.content

    def _imgshape(self, data):
        """Calculate and return image width,height"""
        raise NotImplementedError

    def _get_properties(self):
        p = super(Image,self)._get_properties()
        data = self._img()
        width, height = self._imgshape(data)
        if self.width is not None:
            if self.height is None:
                height = int((self.width/width)*height)
            else:
                height = self.height
            width = self.width
        elif self.height is not None:
            width = int((self.height/height)*width)
            height = self.height
        b64 = base64.b64encode(data).decode("utf-8")
        src = "data:image/"+self.imgtype+";base64,{b64}".format(b64=b64)
        html = "<img src='{src}' width={width} height={height}></img>".format(
            src=src, width=width, height=height
        )
        return dict(p, width=width, height=height, text=html)


class PNG(Image):

    imgtype = 'png'
    
    @classmethod
    def _imgshape(cls, data):
        import struct
        w, h = struct.unpack('>LL', data[16:24])
        return int(w), int(h)


class GIF(Image):

    imgtype = 'gif'

    @classmethod    
    def _imgshape(cls, data):
        import struct
        w, h = struct.unpack("<HH", data[6:10])
        return int(w), int(h)
        

class JPG(Image):

    imgtype = 'jpg'
    
    @classmethod    
    def _imgshape(cls, data):
        import struct
        b = BytesIO(data)
        b.read(2)
        c = b.read(1)
        while (c and ord(c) != 0xDA):
            while (ord(c) != 0xFF): c = b.read(1)
            while (ord(c) == 0xFF): c = b.read(1)
            if (ord(c) >= 0xC0 and ord(c) <= 0xC3):
                b.read(3)
                h, w = struct.unpack(">HH", b.read(4))
                break
            else:
                b.read(int(struct.unpack(">H", b.read(2))[0])-2)
            c = b.read(1)
        return int(w), int(h)


class SVG(Image):

    imgtype = 'svg'

    @classmethod
    def applies(cls, obj):
        return (super(SVG, cls).applies(obj) or
                (isinstance(obj, basestring) and obj.lstrip().startswith('<svg')))

    def _img(self):
        if (isinstance(self.object, basestring) and
            self.object.lstrip().startswith('<svg')):
            return self.object
        return super(SVG, self)._img()

    def _imgshape(self, data):
        return (self.width, self.height)

    def _get_properties(self):
        p = super(Image, self)._get_properties()
        data = self._img()
        width, height = self._imgshape(data)
        if not isinstance(data, bytes):
            data = data.encode('utf-8')
        b64 = base64.b64encode(data).decode("utf-8")
        src = "data:image/svg+xml;base64,{b64}".format(b64=b64)
        html = "<img src='{src}' width={width} height={height}></img>".format(
            src=src, width=width, height=height
        )
        return dict(p, width=width, height=height, text=html)


class Matplotlib(PNG):
    """
    A Matplotlib pane renders a matplotlib figure to png and wraps the
    base64 encoded data in a bokeh Div model. The size of the image in
    pixels is determined by scaling the size of the figure in inches
    by a dpi of 72, increasing the dpi therefore controls the
    resolution of the image not the displayed size.
    """

    dpi = param.Integer(default=144, bounds=(1, None), doc="""
        Scales the dpi of the matplotlib figure.""")

    @classmethod
    def applies(cls, obj):
        if 'matplotlib' not in sys.modules:
            return False
        from matplotlib.figure import Figure
        is_fig = isinstance(obj, Figure)
        if is_fig and obj.canvas is None:
            raise ValueError('Matplotlib figure has no canvas and '
                             'cannot be rendered.')
        return is_fig

    def _imgshape(self, data):
        """Calculate and return image width,height"""
        w, h = self.object.get_size_inches()
        return int(w*72), int(h*72)
    
    def _img(self):
        self.object.set_dpi(self.dpi)
        b = BytesIO()
        self.object.canvas.print_figure(b)
        return b.getvalue()


class RGGPlot(PNG):
    """
    An RGGPlot pane renders an r2py-based ggplot2 figure to png
    and wraps the base64-encoded data in a bokeh Div model.
    """

    height = param.Integer(default=400)

    width = param.Integer(default=400)

    dpi = param.Integer(default=144, bounds=(1, None))

    @classmethod
    def applies(cls, obj):
        return type(obj).__name__ == 'GGPlot' and hasattr(obj, 'r_repr')

    def _img(self):
        from rpy2.robjects.lib import grdevices
        from rpy2 import robjects
        with grdevices.render_to_bytesio(grdevices.png,
                 type="cairo-png", width=self.width, height=self.height,
                 res=self.dpi, antialias="subpixel") as b:
            robjects.r("print")(self.object)
        return b.getvalue()


class HTML(DivPaneBase):
    """
    HTML panes wrap HTML text in a bokeh Div model.  The
    provided object can either be a text string, or an object that
    has a `_repr_html_` method that can be called to get the HTML
    text string.  The height and width can optionally be specified, to
    allow room for whatever is being wrapped.
    """

    precedence = 0.2

    @classmethod
    def applies(cls, obj):
        return (hasattr(obj, '_repr_html_') or
                (isinstance(obj, basestring) or
                 (isinstance(obj, unicode))))

    def _get_properties(self):
        properties = super(HTML, self)._get_properties()
        text=self.object
        if hasattr(text, '_repr_html_'):
            text=text._repr_html_()
        return dict(properties, text=text)


class Str(DivPaneBase):
    """
    A Str pane renders any object for which `str()` can be called,
    escaping any HTML markup and then wrapping the resulting string in
    a bokeh Div model.  Set to a low precedence because generally one
    will want a better representation, but allows arbitrary objects to
    be used as a Pane (numbers, arrays, objects, etc.).
    """

    precedence = 0

    @classmethod
    def applies(cls, obj):
        return True
    
    def _get_properties(self):
        properties = super(Str, self)._get_properties()
        return dict(properties, text='<pre>'+escape(str(self.object))+'</pre>')



class YT(HTML):
    """
    YT panes wrap plottable objects from the YT library.  
    By default, the height and width are calculated by summing all
    contained plots, but can optionally be specified explicitly to
    provide additional space.
    """

    precedence = 0.5

    @classmethod
    def applies(cls, obj):
        return ('yt' in repr(obj) and
                hasattr(obj, "plots") and
                hasattr(obj, "_repr_html_"))

    def _get_properties(self):
        p = super(YT, self)._get_properties()

        width = height = 0
        if self.width  is None or self.height is None:
            for k,v in self.object.plots.items():
                if hasattr(v, "_repr_png_"):
                    img = v._repr_png_()
                    w,h = PNG._imgshape(img)
                    height += h
                    width = max(w, width)
        
        if self.width  is None: p["width"]  = width
        if self.height is None: p["height"] = height

        return p