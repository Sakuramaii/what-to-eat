"""Interpret stated dining intent; estimates are conditions, never invented food facts."""
import re

NUMBER = r'(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百]+)'
MONEY_CUE = r'预算|人均|每人|一顿|花|块|元|[¥￥]|便宜|省钱|节省|实惠|别太贵|不太贵|贵一点|贵点|贵一些|宽裕|不差钱|手头紧|囊中羞涩|吃土|月光'
TIME_CUE = r'分钟|小时|一刻钟|赶时间|赶课|有点赶|来不及|没多少时间|没太多时间|时间.*(?:紧|不多|有限)|快点|快些|别.*太久|尽快|抓紧|没空|就一会|只有一会|不着急|不赶时间|慢慢吃'
PEOPLE_CUE = NUMBER+r'\s*(?:个?人|位)|我们|咱们|俩|双人|独自|就我自己|和.*(?:室友|朋友|同学|同事|家人|对象|师兄|师姐|男朋友|女朋友)|几个人|朋友聚餐'
FIELD_CUES = dict(budget=MONEY_CUE, minutes=TIME_CUE, people=PEOPLE_CUE,
    purpose=r'日常|平常|快|赶|省|便宜|犒劳|奖励|聚餐|朋友|尝|新口味|换.*口味',
    portion=r'饭量|分量|份|饿|少|饱|多|胃口|垫|减脂|不限', pace=r'快|慢|赶|急|节奏|速度|不限',
    foods=r'想吃|要吃|吃点|换成|改吃|面|粉|饭|饺|锅|鸡|鱼|肉|菜|粥|馄饨|包子|披萨|寿司|汉堡|沙拉|烧烤|嗦|品类|餐品|不挑|什么都|随便吃',
    avoid_foods=r'不吃|不要|不想吃|不考虑|别推荐|不点|不限品类|品类不限|餐品不限|不限制餐品|不挑|什么都|随便吃')


def numeral(text):
    if re.fullmatch(r'\d+(?:\.\d+)?',text):
        return float(text)
    digits = dict(zip('零〇一二两三四五六七八九','001223456789'))
    total, digit = 0, 0
    for char in text:
        if char in digits:
            digit = int(digits[char])
        elif char in ['十','百']:
            total += (digit or 1) * (10 if char=='十' else 100)
            digit = 0
        else:
            return None
    return total+digit


def quantities(text,current):
    """Exact written numbers and units, including hours and counts of companions."""
    result = {}
    match = re.search(r'(?:人均|预算(?:总共|一共)?|每人|一顿(?:花)?|最多(?:花)?|不超过|控制在|花)\s*('+NUMBER+r')\s*(?:元|块)?',text)
    if match:
        result['budget'] = numeral(match[1])
    match = re.search('('+NUMBER+r')\s*分钟',text)
    if match:
        result['minutes'] = numeral(match[1])
    else:
        match = re.search('('+NUMBER+r')\s*(?:个)?半小时',text)
        if match:
            result['minutes'] = numeral(match[1])*60+30
        else:
            match = re.search('('+NUMBER+r')\s*(?:个)?小时(半)?',text)
            if match:
                result['minutes'] = numeral(match[1])*60+(30 if match[2] else 0)
            elif re.search(r'半个?小时',text):
                result['minutes'] = 30
            elif '一刻钟' in text:
                result['minutes'] = 15
    match = re.search('('+NUMBER+r')\s*(?:个?人|位)',text)
    if match:
        result['people'] = numeral(match[1])
    if re.search(r'我们俩|咱俩|咱们俩|双人|两位',text):
        result['people'] = 2
    elif re.search(r'独自|就我自己|只有我一个|我一个人',text):
        result['people'] = 1
    match = re.search(r'(?:我)?和('+NUMBER+r')(?:个|位)(?:朋友|室友|同学|同事)',text)
    if match:
        result['people'] = numeral(match[1])+1
    if result.get('budget') is not None and re.search(r'总预算|预算总共|预算一共|预算总额',text) and not re.search(r'人均|每人',text):
        people = result.get('people') or current.get('people')
        if people:
            result['budget'] = round(result['budget']/people,2)
        else:
            result.pop('budget')
    return result


def estimates(text,current):
    values, evidence = {}, {}
    def put(field,value,quote,explanation,approximate=False):
        values[field] = value
        evidence[field] = dict(value=value,reason='根据“'+quote+'”，'+explanation,approximate=approximate)

    fuzzy_money = r'(十几|二十来|二三十|三四十|四五十|几十)'
    match = (re.search(fuzzy_money+r'\s*(?:块|元)',text) or
             re.search(r'(?:预算|人均|每人|花)\s*'+fuzzy_money,text))
    if match:
        value = {'十几':15,'二十来':20,'二三十':25,'三四十':35,'四五十':45,'几十':30}[match[1]]
        put('budget',value,match[0],f'暂按人均 {value} 元理解',True)
    elif re.search(r'便宜|省钱|节省|实惠|别太贵|不要太贵|不太贵|手头紧|囊中羞涩|吃土|月光',text) and not re.search(r'不(?:用|必|想|要)(?:太)?省钱|不考虑价格|预算不限|不限预算|不设预算',text):
        quote = re.search(r'便宜(?:一点|一些|点)?|省钱|节省|实惠|别太贵|不要太贵|不太贵|手头紧|囊中羞涩|吃土|月光',text)[0]
        previous = current.get('budget')
        value = max(1,int(previous*.8)) if previous else 15
        put('budget',value,quote,f'先按人均 {value} 元筛选，可修改')
    elif re.search(r'贵一点|贵点|贵一些|预算宽裕|不差钱',text):
        quote = re.search(r'贵一点|贵点|贵一些|预算宽裕|不差钱',text)[0]
        value = min(500,round(current['budget']*1.25/5)*5) if current.get('budget') else 35
        put('budget',value,quote,f'暂按人均 {value} 元理解，可修改')
    match = re.search(r'(十几|二十来|二三十|三四十|四五十|几)\s*分钟',text)
    if match:
        value = {'十几':15,'二十来':20,'二三十':25,'三四十':35,'四五十':45,'几':10}[match[1]]
        put('minutes',value,match[0],f'暂按 {value} 分钟理解',True)
    elif re.search(r'赶时间|赶课|有点赶|来不及|没多少时间|没太多时间|时间(?:有点|比较|很)?紧|快点|快些|别太久|尽快|抓紧',text) and not re.search(r'不赶时间|不着急|不用太快|不需要.*快|不一定.*快|慢慢吃|慢点吃',text):
        quote = re.search(r'赶时间|赶课|有点赶|来不及|没多少时间|没太多时间|时间(?:有点|比较|很)?紧|快点|快些|别太久|尽快|抓紧',text)[0]
        value = min(20,current['minutes']) if current.get('minutes') else 20
        put('minutes',value,quote,f'暂按 {value} 分钟、尽快用餐理解；实际耗时需核实')
        values['pace'] = '快点吃'
        values['purpose'] = '快速吃饱'
    match = re.search(r'(两三|三四|四五|五六|几)(?:个?人|位)',text)
    if match:
        value = {'两三':3,'三四':4,'四五':5,'五六':6,'几':3}[match[1]]
        put('people',value,match[0],f'先按 {value} 人准备份量，可修改',True)
    elif re.search(r'(?:我)?和(?:室友|朋友|同学|同事|对象|师兄|师姐|男朋友|女朋友)(?:一起)?',text):
        quote = re.search(r'(?:我)?和(?:室友|朋友|同学|同事|对象|师兄|师姐|男朋友|女朋友)(?:一起)?',text)[0]
        put('people',2,quote,'先按你和一位同行者、共 2 人理解')
    if re.search(r'没什么胃口|没胃口|垫垫|垫一下|垫一垫|随便吃点|减脂',text):
        quote = re.search(r'没什么胃口|没胃口|垫垫|垫一下|垫一垫|随便吃点|减脂',text)[0]
        put('portion','少一些',quote,'暂按少吃一些理解')
    elif re.search(r'饿坏|饿死|饿得|胃口大|饿了一天',text):
        quote = re.search(r'饿坏|饿死|饿得|胃口大|饿了一天',text)[0]
        put('portion','吃饱',quote,'暂按希望吃饱理解')
    for field,unit in [('budget',r'元|块'),('minutes','分钟'),('people',r'人|位')]:
        match = re.search('('+NUMBER+r')\s*(?:'+unit+r')\s*(?:左右|上下)',text)
        if match and (field in quantities(text,current)):
            value = quantities(text,current)[field]
            if numeral(match[1]) == value:
                put(field,value,match[0],'按大致数值理解，可修改',True)
    return values,evidence
