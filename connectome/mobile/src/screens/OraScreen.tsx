import React, { useState, useRef } from 'react';
import {
  View,
  Text,
  TextInput,
  TouchableOpacity,
  FlatList,
  StyleSheet,
  KeyboardAvoidingView,
  Platform,
  ActivityIndicator,
  SafeAreaView,
} from 'react-native';
import { chatWithOra, ChatMessage } from '../lib/api';

interface DisplayMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
}

export default function OraScreen() {
  const [messages, setMessages] = useState<DisplayMessage[]>([
    {
      id: '0',
      role: 'assistant',
      content: "Hi! I'm Ora, your personal growth guide. What's on your mind?",
    },
  ]);
  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);
  const listRef = useRef<FlatList>(null);

  const send = async () => {
    const text = input.trim();
    if (!text || sending) return;
    setInput('');

    const userMsg: DisplayMessage = { id: Date.now().toString(), role: 'user', content: text };
    setMessages((prev) => [...prev, userMsg]);

    setSending(true);
    try {
      const history: ChatMessage[] = messages
        .filter((m) => m.id !== '0')
        .map((m) => ({ role: m.role, content: m.content }));
      history.push({ role: 'user', content: text });

      const reply = await chatWithOra(text, history);
      const oraMsg: DisplayMessage = {
        id: (Date.now() + 1).toString(),
        role: 'assistant',
        content: reply || "I'm here \u2014 tell me more.",
      };
      setMessages((prev) => [...prev, oraMsg]);
    } catch {
      const errMsg: DisplayMessage = {
        id: (Date.now() + 1).toString(),
        role: 'assistant',
        content: 'Something went wrong. Try again.',
      };
      setMessages((prev) => [...prev, errMsg]);
    } finally {
      setSending(false);
      setTimeout(() => listRef.current?.scrollToEnd({ animated: true }), 100);
    }
  };

  const renderItem = ({ item }: { item: DisplayMessage }) => {
    const isUser = item.role === 'user';
    return (
      <View style={[styles.bubble, isUser ? styles.bubbleUser : styles.bubbleOra]}>
        {!isUser && <Text style={styles.oraLabel}>Ora</Text>}
        <Text style={[styles.bubbleText, isUser ? styles.bubbleTextUser : styles.bubbleTextOra]}>
          {item.content}
        </Text>
      </View>
    );
  };

  return (
    <SafeAreaView style={styles.container}>
      <View style={styles.header}>
        <View style={styles.oraDot} />
        <Text style={styles.headerTitle}>Ora</Text>
        <Text style={styles.headerSub}>your growth guide</Text>
      </View>

      <FlatList
        ref={listRef}
        data={messages}
        keyExtractor={(item) => item.id}
        renderItem={renderItem}
        contentContainerStyle={styles.list}
        onContentSizeChange={() => listRef.current?.scrollToEnd({ animated: false })}
      />

      <KeyboardAvoidingView
        behavior={Platform.OS === 'ios' ? 'padding' : undefined}
      >
        <View style={styles.inputRow}>
          <TextInput
            style={styles.textInput}
            placeholder="Message Ora…"
            placeholderTextColor="#555"
            value={input}
            onChangeText={setInput}
            multiline
            maxLength={500}
            onSubmitEditing={send}
          />
          <TouchableOpacity
            style={[styles.sendBtn, (!input.trim() || sending) && styles.sendBtnDisabled]}
            onPress={send}
            disabled={!input.trim() || sending}
          >
            {sending ? (
              <ActivityIndicator size="small" color="#fff" />
            ) : (
              <Text style={styles.sendIcon}>↑</Text>
            )}
          </TouchableOpacity>
        </View>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: '#0a0a0a' },
  header: {
    flexDirection: 'row',
    alignItems: 'center',
    paddingHorizontal: 20,
    paddingVertical: 14,
    borderBottomWidth: 1,
    borderBottomColor: '#1a1a1a',
    gap: 8,
  },
  oraDot: {
    width: 10,
    height: 10,
    borderRadius: 5,
    backgroundColor: '#8b5cf6',
  },
  headerTitle: { color: '#fff', fontSize: 18, fontWeight: '700' },
  headerSub: { color: '#555', fontSize: 13, marginLeft: 2 },
  list: { padding: 16, gap: 12 },
  bubble: {
    maxWidth: '80%',
    borderRadius: 16,
    padding: 12,
    marginVertical: 4,
  },
  bubbleUser: {
    alignSelf: 'flex-end',
    backgroundColor: '#8b5cf6',
    borderBottomRightRadius: 4,
  },
  bubbleOra: {
    alignSelf: 'flex-start',
    backgroundColor: '#1a1a1a',
    borderBottomLeftRadius: 4,
    borderWidth: 1,
    borderColor: '#2a2a2a',
  },
  oraLabel: { color: '#8b5cf6', fontSize: 11, fontWeight: '600', marginBottom: 4 },
  bubbleText: { fontSize: 15, lineHeight: 22 },
  bubbleTextUser: { color: '#fff' },
  bubbleTextOra: { color: '#ddd' },
  inputRow: {
    flexDirection: 'row',
    alignItems: 'flex-end',
    paddingHorizontal: 16,
    paddingVertical: 12,
    gap: 10,
    borderTopWidth: 1,
    borderTopColor: '#1a1a1a',
  },
  textInput: {
    flex: 1,
    backgroundColor: '#1a1a1a',
    color: '#fff',
    borderRadius: 20,
    paddingHorizontal: 16,
    paddingVertical: 10,
    fontSize: 15,
    maxHeight: 120,
    borderWidth: 1,
    borderColor: '#2a2a2a',
  },
  sendBtn: {
    width: 40,
    height: 40,
    borderRadius: 20,
    backgroundColor: '#8b5cf6',
    justifyContent: 'center',
    alignItems: 'center',
  },
  sendBtnDisabled: { backgroundColor: '#3a2a5a' },
  sendIcon: { color: '#fff', fontSize: 18, fontWeight: '700' },
});
