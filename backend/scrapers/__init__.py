from .ntuc        import search_ntuc
from .shengsiong  import search_shengsiong
from .giant       import search_giant
from .coldstorage import search_coldstorage
from .redmart     import search_redmart

SCRAPERS = {
    "ntuc":  search_ntuc,
    "sheng": search_shengsiong,
    "giant": search_giant,
    "cold":  search_coldstorage,
    "red":   search_redmart,
}
