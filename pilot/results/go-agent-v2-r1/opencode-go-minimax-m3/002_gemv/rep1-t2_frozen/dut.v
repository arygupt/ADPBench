module dut #(
    parameter ROWS = 16,
    parameter COLS = 64,
    parameter LANES = 16,
    parameter DATA_W = 8,
    parameter ACC_W = 32
)(
    input  wire                    clk,
    input  wire                    rst_n,
    input  wire [LANES*DATA_W-1:0] in_a_flat,
    input  wire                    in_a_flat_valid,
    output wire                    in_a_flat_ready,
    input  wire [LANES*DATA_W-1:0] in_x_flat,
    input  wire                    in_x_flat_valid,
    output wire                    in_x_flat_ready,
    output wire                    out_valid,
    input  wire                    out_ready,
    output wire signed [ACC_W-1:0] out_c
);

typedef enum logic [1:0] {
    S_IDLE,
    S_COMPUTE,
    S_OUTPUT
} state_t;

state_t state;

// Storage
logic signed [LANES*DATA_W-1:0] x_beat [0:3];   // 4 x beats
logic signed [LANES*DATA_W-1:0] a_beat;         // current A beat
logic signed [ACC_W-1:0]        acc   [0:ROWS-1]; // 16 accumulators

// Counters
logic [2:0] x_count;
logic [6:0] a_count;
logic [4:0] out_count;
logic [3:0] row_idx;
logic [1:0] col_idx;

// Pipeline registers for products (helps with DSP inference)
logic signed [2*DATA_W-1:0] p [0:LANES-1];
logic signed [ACC_W-1:0]    row_sum;

always_comb begin
    for (int i = 0; i < LANES; i++) begin
        p[i] = $signed(a_beat[i*8 +: 8]) * $signed(x_beat[col_idx][i*8 +: 8]);
    end
end

always_comb begin
    row_sum = '0;
    for (int i = 0; i < LANES; i++) begin
        row_sum += $signed({{(ACC_W-2*DATA_W){p[i][2*DATA_W-1]}}, p[i]});
    end
end

// Handshakes
assign in_x_flat_ready = (state == S_IDLE);
assign in_a_flat_ready = (state == S_COMPUTE) && (a_count < 7'd64);
assign out_valid       = (state == S_OUTPUT);
assign out_c           = acc[out_count];

// FSM
always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
        state     <= S_IDLE;
        x_count   <= '0;
        a_count   <= '0;
        out_count <= '0;
        row_idx   <= '0;
        col_idx   <= '0;
        a_beat    <= '0;
        for (int i = 0; i < 4; i++)    x_beat[i] <= '0;
        for (int i = 0; i < ROWS; i++) acc[i]    <= '0;
    end else begin
        case (state)
            S_IDLE: begin
                if (in_x_flat_valid && in_x_flat_ready) begin
                    x_beat[x_count] <= in_x_flat;
                    if (x_count == 2'd3) begin
                        state    <= S_COMPUTE;
                        a_count  <= '0;
                        row_idx  <= '0;
                        col_idx  <= '0;
                        for (int i = 0; i < ROWS; i++) acc[i] <= '0;
                    end else begin
                        x_count  <= x_count + 1;
                    end
                end
            end

            S_COMPUTE: begin
                if (in_a_flat_valid && in_a_flat_ready) begin
                    a_beat <= in_a_flat;
                    if (a_count != '0) begin
                        acc[row_idx] <= acc[row_idx] + row_sum;
                        if (col_idx == 2'd3) begin
                            row_idx <= row_idx + 1;
                            col_idx <= '0;
                        end else begin
                            col_idx <= col_idx + 1;
                        end
                    end
                    a_count <= a_count + 1;
                end else if (a_count == 7'd64) begin
                    acc[row_idx] <= acc[row_idx] + row_sum;
                    state     <= S_OUTPUT;
                    out_count <= '0;
                end
            end

            S_OUTPUT: begin
                if (out_valid && out_ready) begin
                    out_count <= out_count + 1;
                    if (out_count == 4'd15) begin
                        state    <= S_IDLE;
                        x_count  <= '0;
                        out_count<= '0;
                    end
                end
            end

            default: state <= S_IDLE;
        endcase
    end
end

endmodule