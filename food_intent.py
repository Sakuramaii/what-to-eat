"""Current-meal food requests, separate from flavors and long-term preferences."""
import re


ALIASES = {'面条':'面', '面食':'面', '面类':'面', '粉类':'米粉', '米线':'米粉',
           '饭类':'饭', '米饭类':'饭', '麻辣香锅':'香锅'}
FOODS = ['牛拉面','米粉','米线','粉面','肠粉','拌面','拉面','汤面','面条','面食','面类','面',
         '炒饭','烤盘饭','木桶饭','煲仔饭','套餐饭','盖饭','米饭类','米饭','饭类','饭',
         '麻辣香锅','香锅','麻辣烫','小火锅','火锅','饺子','烧腊','烧卤','海南鸡','酸菜鱼',
         '跷脚牛肉','豆花','铁板','自选','熟食','快餐','江西','东北','澳门','川湘','韩式','锅仔',
         '披萨','寿司','汉堡','炸鸡','沙拉','馄饨','包子','烧烤','粥']
FOOD_PATTERN = re.compile('|'.join(re.escape(word) for word in sorted(FOODS,key=len,reverse=True)))
INTENT = re.compile(r'(?P<negative>不想吃|不要吃|不吃|不考虑|不想要|别推荐|不要|不点)|'
                    r'(?P<positive>想吃|要吃|吃点|吃个|来份|来一份|换成|改成|改吃|就吃|想点|想来|推荐|有没有|能吃|想要|点个|来个|来碗|来一碗|想喝|喝点|来杯)')


def canonical_food(value):
    value = value.strip()
    return ALIASES.get(value,value)


def matches_food(food, item):
    needle = canonical_food(food).casefold()
    names = [str(item.get(field,'')) for field in ['name','category']]
    return any(needle in canonical_food(name).casefold() for name in names)


def food_changes(text, current):
    positives, negatives = [], []
    clear = bool(re.search(r'不限品类|品类不限|餐品不限|不限制餐品|什么都(?:行|可以)|不挑(?:食)?|随便吃',text))
    for clause in re.split(r'[，,。；;！!\n]',text):
        for match in FOOD_PATTERN.finditer(clause):
            prefix, suffix = clause[:match.start()], clause[match.end():]
            if match[0]=='饭' and re.search(r'(?:饱|顿|顿好)$',prefix):
                continue
            cues = list(INTENT.finditer(prefix))
            direction = None
            if cues:
                direction = 'negative' if cues[-1].lastgroup=='negative' else 'positive'
            elif re.match(r'\s*(?:也)?(?:不吃|不要|不想吃)',suffix):
                direction = 'negative'
            elif not re.search(r'上次|上一餐|昨天|之前|刚才|刚刚',clause) and (
                    clause.strip()==match[0] or re.search(r'推荐|有哪些|有什么|都可以|也可以|也行',suffix)):
                direction = 'positive'
            if direction:
                (negatives if direction=='negative' else positives).append(canonical_food(match[0]))
    if not positives and not negatives and not clear:
        return {}
    foods = list(dict.fromkeys(positives)) if positives else ([] if clear else list(current.get('foods',[])))
    excluded = [] if clear else list(current.get('avoid_foods',[]))
    # A new explicit request replaces an earlier exclusion of the same food;
    # more specific exclusions (e.g. 面 but not 拉面) remain meaningful.
    excluded = [word for word in excluded if canonical_food(word) not in positives]
    excluded = list(dict.fromkeys(excluded+negatives))
    foods = [word for word in foods if canonical_food(word) not in negatives]
    changes = {}
    if positives or clear or foods!=current.get('foods',[]):
        changes['foods'] = foods
    if negatives or clear or excluded!=current.get('avoid_foods',[]):
        changes['avoid_foods'] = excluded
    return changes
