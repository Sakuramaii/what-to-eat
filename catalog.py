"""User-provided canteen descriptions, without invented prices or minutes."""
import csv
import hashlib
from pathlib import Path
import re
from descriptions import METRIC_TERMS, FEEDBACK_TERMS, metric_description

TASTES = ['清淡', '微辣', '香辣', '鲜香', '酸甜', '浓郁']
PURPOSES = ['日常用餐', '快速吃饱', '节省开支', '犒劳自己', '朋友聚餐', '尝试新口味']
PORTIONS = ['不限', '少一些', '适中', '吃饱']
PACES = ['不限', '慢慢吃', '正常', '快点吃']
CATALOG_VERSION = 'personal-canteens-text-v2'
PRICE_BANDS = {'便宜':(0,1000),'较便宜':(1000,1500),'适中':(1500,2000),'较贵':(2000,2500),'贵':(2500,None)}
SCORE_FIELDS = ['distance','price','taste','portion','speed']


def load_rows():
    result=[]
    with Path(__file__).with_name('canteens.tsv').open(encoding='utf-8',newline='') as file:
        for row in list(csv.reader(file,delimiter='\t'))[1:]:
            if not row or '*' in row[0] or len(row)!=6:
                continue
            descriptions=[metric_description(field,value) for field,value in zip(SCORE_FIELDS,row[1:])]
            name=row[0]
            match=re.fullmatch(r'(清芬|清青|听涛|紫荆|桃李|观畴|荷园|丁香|玉树|芝兰|澜园)(一楼|二楼|三楼|四楼|地下)?(.*)',name)
            if not match:
                raise ValueError('无法读取食堂名称：'+name)
            canteen,floor,food=match.groups()
            floor=floor or ''
            if not food:
                food,floor=floor,''
            result.append(dict(id='personal-'+hashlib.sha256(name.encode()).hexdigest()[:12],
                               full_name=name,canteen=canteen,floor=floor,food=food,
                               **dict(zip(SCORE_FIELDS,descriptions))))
    return result


CANTEEN_ROWS=load_rows()


def rated_venue(row):
    ratings={field:metric_description(field,row[field]) for field in SCORE_FIELDS}
    low,high=PRICE_BANDS[ratings['price']]
    dish=dict(id=row['id']+'-meal',name=row['food'],price=None,taste='未注明',ingredients=[],
              ingredients_known=False,category=row['food'],kind='meal',portion=ratings['portion'],
              price_low=low,price_high=high)
    return dict(id=row['id'],name=row['canteen']+(' · '+row['floor'] if row['floor'] else ''),
                full_name=row['full_name'],scene='canteen',cuisine=row['food'],art='bowls',rated=True,
                ratings=ratings,walk=None,wait=None,dine=None,description='来自你的常吃食堂统计；距离和吃完速度为主观描述',dishes=[dish])


VENUES=[rated_venue(row) for row in CANTEEN_ROWS]
VENUE_MAP={v['id']:v for v in VENUES}
DISH_MAP={d['id']:dict(d,venue_id=v['id']) for v in VENUES for d in v['dishes']}
# Generic categories retain meaningful feedback from previously saved meals and uploaded menus.
MEMORY_TAGS=sorted(set(TASTES+[row['food'] for row in CANTEEN_ROWS]+['家常饭','面食','素食','蒸食','川菜','日料','轻食','东南亚菜']))


def public_catalog():
    return dict(venues=VENUES,tastes=TASTES,purposes=PURPOSES,portions=PORTIONS,paces=PACES,memory_tags=MEMORY_TAGS,
                sample=False,source='personal_descriptions',entry_count=len(CANTEEN_ROWS),
                descriptors=METRIC_TERMS,feedback_terms=FEEDBACK_TERMS,
                notice='你的常吃食堂统计 · 五项指标以文字描述，价格为区间，未提供实际分钟数')
