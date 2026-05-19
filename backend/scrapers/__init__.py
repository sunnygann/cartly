from .ntuc        import search_ntuc
from .shengsiong  import search_shengsiong
from .giant       import search_giant
from .coldstorage import search_coldstorage
from .redmart     import search_redmart
from .donki       import search_donki
from .amazon      import search_amazon
from .grabmart    import search_grabmart

SCRAPERS = {
    "ntuc":   search_ntuc,
    "sheng":  search_shengsiong,
    "giant":  search_giant,
    "cold":   search_coldstorage,
    "red":    search_redmart,
    "donki":  search_donki,
    "amazon": search_amazon,
    "grab":   search_grabmart,
}
